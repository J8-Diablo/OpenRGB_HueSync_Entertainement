#!/usr/bin/env python3
"""
Hue ↔ E1.31 Bridge for OpenRGB (Entertainment API - Ultra Low Latency)

This script uses the Hue Entertainment API (UDP/DTLS streaming) for zero-lag control.
Much faster than HTTP REST API - same performance as Hue Sync!

Requirements:
  pip install requests sacn hue-entertainment-pykit

Usage:
  python hue_e131_entertainment.py                 # Auto-discover Bridge
  python hue_e131_entertainment.py --re-pair       # Re-pair to get clientkey (press button!)
  python hue_e131_entertainment.py --list          # List entertainment areas
  python hue_e131_entertainment.py --area "Name"   # Start bridge
  python hue_e131_entertainment.py -v              # Verbose mode

Setup:
  1. Run with --re-pair and press the bridge button to get clientkey
  2. Create an Entertainment Area in the Hue app (Settings → Entertainment areas)
  3. Run with --list to see available entertainment areas
  4. Run with --area "Area Name" to start the bridge
  5. Configure OpenRGB with the universe number shown
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Setup logging
logger = logging.getLogger("HueE131Entertainment")
handler = logging.StreamHandler()
formatter = logging.Formatter('[%(levelname)s] %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.WARNING)

try:
    import requests
except ImportError:
    print("ERROR: 'requests' package required. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    import sacn
except ImportError:
    print("ERROR: 'sacn' package required. Install with: pip install sacn", file=sys.stderr)
    sys.exit(1)

try:
    from hue_entertainment_pykit import create_bridge, Entertainment, Streaming
except ImportError:
    print("ERROR: 'hue-entertainment-pykit' package required.", file=sys.stderr)
    print("Install with: pip install hue-entertainment-pykit", file=sys.stderr)
    sys.exit(1)

requests.packages.urllib3.disable_warnings()

NUPNP_DISCOVERY = "https://discovery.meethue.com"

# --------------------------- Config persistence --------------------------- #

def _default_config_path() -> Path:
    if os.name == "nt":
        base = os.getenv("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        return Path(base) / "hue_cli" / "config.json"
    else:
        return Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "hue_cli" / "config.json"

CONFIG_PATH = _default_config_path()


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"Warning: failed to read config {path}: {e}", file=sys.stderr)
        return {}


def save_config(data: Dict[str, Any], path: Path = CONFIG_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Warning: failed to write config {path}: {e}", file=sys.stderr)


# ----------------------------- Discovery ---------------------------------- #

def discover_bridge_ip() -> Optional[str]:
    """Fallback discovery using NUPNP."""
    try:
        r = requests.get(NUPNP_DISCOVERY, timeout=5)
        r.raise_for_status()
        arr = r.json()
        if isinstance(arr, list) and arr:
            ip = arr[0].get("internalipaddress")
            if ip:
                return ip
    except Exception as e:
        print(f"Discovery via {NUPNP_DISCOVERY} failed: {e}", file=sys.stderr)
    return None


def readable_host() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return "python-client"


# ---------------------------- Pairing / Keys ------------------------------ #

def create_entertainment_user(bridge_ip: str) -> Tuple[str, str]:
    """
    Create a new user with clientkey for Entertainment API.
    Requires pressing the link button on the bridge!
    Returns: (username, clientkey)
    """
    print("\n" + "="*70)
    print("🔵 ENTERTAINMENT API PAIRING")
    print("="*70)
    print("\n⚠️  IMPORTANT: Go to your Hue Bridge and press the LINK BUTTON now!")
    print("   You have 30 seconds...\n")
    print("   Waiting for button press", end="", flush=True)
    
    devicetype = f"OpenRGB app Bridge ({readable_host()})"
    url = f"http://{bridge_ip}/api"
    payload = {
        "devicetype": devicetype,
        "generateclientkey": True  # THIS IS THE KEY! 🔑
    }

    deadline = time.time() + 30
    last_err: Optional[str] = None
    dots = 0
    
    while time.time() < deadline:
        try:
            resp = requests.post(url, json=payload, timeout=3)
            resp.raise_for_status()
            data = resp.json()
            
            if isinstance(data, list) and len(data) > 0:
                item = data[0]
                
                if "success" in item:
                    success = item["success"]
                    username = success.get("username")
                    clientkey = success.get("clientkey")
                    
                    if username and clientkey:
                        print("\n\n" + "="*70)
                        print("✅ SUCCESS! Entertainment credentials created!")
                        print("="*70)
                        print(f"\nUsername:  {username}")
                        print(f"Clientkey: {clientkey}")
                        print("\n💾 These will be saved to your config file.")
                        print("="*70 + "\n")
                        return username, clientkey
                
                if "error" in item:
                    error_desc = item["error"].get("description", "")
                    last_err = error_desc
                    
                    # Don't spam the "button not pressed" message
                    if "link button not pressed" not in error_desc.lower():
                        print(f"\nError: {error_desc}")
            
        except requests.exceptions.RequestException as e:
            last_err = str(e)
        
        # Animate waiting
        dots = (dots + 1) % 4
        print(f"\r   Waiting for button press{'.' * dots}   ", end="", flush=True)
        time.sleep(2)

    print("\n\n" + "="*70)
    print("❌ TIMEOUT - Button was not pressed in time")
    print("="*70)
    if last_err:
        print(f"\nLast error: {last_err}")
    print("\nPlease try again with --re-pair")
    print("="*70 + "\n")
    
    raise RuntimeError("Failed to create Entertainment credentials. Did you press the link button?")


def ensure_app_key(bridge_ip: str, app_key: Optional[str], cfg: Dict[str, Any], force_repair: bool = False) -> Tuple[str, str, Dict[str, Any]]:
    """
    Ensure we have both username and clientkey for Entertainment API.
    Returns: (username, clientkey, updated_config)
    """
    
    # If force re-pairing, create new credentials
    if force_repair:
        print("\n🔄 Force re-pairing requested. Old credentials will be replaced.\n")
        username, clientkey = create_entertainment_user(bridge_ip)
        cfg["app_key"] = username
        cfg["clientkey"] = clientkey
        cfg["updated_at"] = datetime.utcnow().isoformat() + "Z"
        return username, clientkey, cfg
    
    # Check if we have both username and clientkey
    username = app_key or cfg.get("app_key")
    clientkey = cfg.get("clientkey")
    
    if username and clientkey:
        logger.info("Using existing Entertainment credentials")
        return username, clientkey, cfg
    
    # If we have username but no clientkey, we need to re-pair
    if username and not clientkey:
        print("\n⚠️  You have a username but no clientkey!")
        print("   The clientkey is required for Entertainment API.\n")
        print("   Run with --re-pair to create Entertainment credentials.\n")
        raise RuntimeError("Missing clientkey. Run with --re-pair")
    
    # No credentials at all
    print("\n⚠️  No Entertainment credentials found!")
    print("   Run with --re-pair to create them.\n")
    raise RuntimeError("No credentials found. Run with --re-pair")


# ------------------------------- HTTP ------------------------------------- #

def hue_get(bridge_ip: str, app_key: str, path: str) -> Dict[str, Any]:
    url = f"https://{bridge_ip}{path}"
    headers = {"hue-application-key": app_key}
    r = requests.get(url, headers=headers, timeout=5, verify=False)
    r.raise_for_status()
    return r.json()


# ----------------------- Bridge Info Discovery ---------------------------- #

def get_bridge_info(bridge_ip: str, app_key: str) -> Dict[str, Any]:
    """Get bridge information needed for Entertainment API."""
    try:
        # Get bridge config
        data = hue_get(bridge_ip, app_key, "/clip/v2/resource/bridge")
        if data.get("data") and len(data["data"]) > 0:
            bridge_data = data["data"][0]
            
            return {
                "id": bridge_data.get("id"),
                "rid": bridge_data.get("owner", {}).get("rid") or bridge_data.get("id"),
                "name": (bridge_data.get("metadata") or {}).get("name", "Hue Bridge"),
                "swversion": 1962097030,
                "hue_app_id": bridge_data.get("id"),
            }
    except Exception as e:
        logger.warning(f"Could not get full bridge info: {e}")
    
    # Fallback to minimal info
    return {
        "id": "default-bridge-id",
        "rid": "default-bridge-rid",
        "name": "Hue Bridge",
        "swversion": 1962097030,
        "hue_app_id": "default-app-id",
    }


def get_entertainment_area_names(bridge_ip: str, app_key: str) -> Dict[str, str]:
    """Get human-readable names for entertainment areas. Returns dict: id -> name"""
    try:
        data = hue_get(bridge_ip, app_key, "/clip/v2/resource/entertainment_configuration")
        names = {}
        for area in data.get("data", []):
            area_id = area.get("id")
            area_name = area.get("metadata", {}).get("name", area_id)
            names[area_id] = area_name
        return names
    except Exception as e:
        logger.warning(f"Could not get entertainment area names: {e}")
        return {}


# ------------------------------ E1.31 Bridge ------------------------------ #

class HueEntertainmentBridge:
    def __init__(self, bridge_config: Any, entertainment_area_name: str, universe: int = 1):
        self.bridge = bridge_config
        self.universe = universe
        self.packet_count = 0
        self.streaming = None
        self.entertainment_config = None
        self.bridge_name = getattr(bridge_config, 'name', 'Hue Bridge')
        
        logger.info(f"Initializing Entertainment API connection to bridge '{self.bridge_name}'")
        
        # Set up the Entertainment API service
        try:
            self.entertainment_service = Entertainment(self.bridge)
            logger.info("✅ Entertainment service initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Entertainment service: {e}")
            raise
        
        # Fetch all Entertainment Configurations
        try:
            entertainment_configs = self.entertainment_service.get_entertainment_configs()
            logger.info(f"Found {len(entertainment_configs)} entertainment configurations")
            
            # Find the selected area by name or ID
            for area_id, config in entertainment_configs.items():
                # Try matching by ID first
                if area_id.lower() == entertainment_area_name.lower():
                    self.entertainment_config = config
                    self.area_name = entertainment_area_name
                    break
                # If not found, the library might use the name as key
                if area_id.lower() == entertainment_area_name.lower():
                    self.entertainment_config = config
                    self.area_name = area_id
                    break
            
            if not self.entertainment_config:
                raise ValueError(f"Entertainment area '{entertainment_area_name}' not found")
            
            logger.info(f"Selected entertainment area: {self.area_name}")
            
        except Exception as e:
            logger.error(f"Failed to get entertainment configurations: {e}")
            raise
        
        # Set up the Streaming service
        try:
            self.streaming = Streaming(
                self.bridge,
                self.entertainment_config,
                self.entertainment_service.get_ent_conf_repo()
            )
            logger.info("✅ Streaming service initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Streaming service: {e}")
            raise
        
        # Get number of lights in the entertainment area
        self.num_lights = len(self.entertainment_config.channels)
        logger.info(f"Number of lights in area: {self.num_lights}")
        
        # Get local IP for diagnostic
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            logger.info(f"Local IP: {local_ip}")
        except Exception:
            logger.warning("Could not determine local IP")
        
        # Create E1.31 receiver
        self.receiver = sacn.sACNreceiver(bind_address="0.0.0.0")
        logger.info("E1.31 receiver created (listening on 0.0.0.0:5568)")
        
        def callback(packet):
            self._on_dmx_data(packet)
        
        self.receiver.join_multicast(self.universe)
        logger.info(f"Joined multicast group for universe {self.universe}")
        
        self.receiver.register_listener('universe', callback, universe=self.universe)
        logger.info(f"Registered listener for universe {self.universe}")
        
        self.receiver.start()
        logger.info("✅ E1.31 receiver started and listening")
        logger.info(f"📡 Multicast address: 239.255.{(self.universe >> 8) & 0xFF}.{self.universe & 0xFF}:5568")
        
    def _on_dmx_data(self, packet):
        """Called when E1.31 data is received - streams to Entertainment API."""
        try:
            self.packet_count += 1
            dmx = packet.dmxData
            
            # Log first packet
            if self.packet_count == 1:
                logger.info("=" * 70)
                logger.info("🎉 FIRST PACKET RECEIVED!")
                logger.info("=" * 70)
                logger.info(f"Universe: {packet.universe}")
                logger.info(f"DMX Data Length: {len(dmx)} bytes")
                logger.info(f"First 20 bytes: {list(dmx[:20])}")
                logger.info("=" * 70)
            
            if self.packet_count % 500 == 0:  # Reduced logging frequency
                logger.info(f"📦 Received {self.packet_count} packets (streaming via UDP)")
            
            logger.debug(f"📦 Packet #{self.packet_count}: {len(dmx)} bytes")
            
            # Process each light (3 channels per LED: R, G, B)
            # Optimized: batch all inputs before processing
            for idx in range(self.num_lights):
                channel_offset = idx * 3
                
                if len(dmx) >= channel_offset + 3:
                    r = dmx[channel_offset + 0]
                    g = dmx[channel_offset + 1]
                    b = dmx[channel_offset + 2]
                    
                    # Convert RGB (0-255) to RGB (0.0-1.0) for Entertainment API
                    # Optimized: direct division without intermediate variables
                    self.streaming.set_input((r / 255.0, g / 255.0, b / 255.0, idx))
                    
                    if self.packet_count <= 2:  # Show less initial logs
                        logger.info(f"🎨 Light {idx+1}: RGB({r},{g},{b})")
            
        except Exception as e:
            logger.error(f"Error processing E1.31 data: {e}", exc_info=True)
    
    def start_streaming(self):
        """Start the Entertainment API streaming session."""
        try:
            logger.info("🚀 Starting Entertainment streaming session...")
            self.streaming.start_stream()
            # Set color space to RGB
            self.streaming.set_color_space("rgb")
            logger.info("✅ Entertainment streaming active (RGB color space)!")
        except Exception as e:
            logger.error(f"Failed to start streaming: {e}")
            raise
    
    def stop_streaming(self):
        """Stop the Entertainment API streaming session."""
        try:
            logger.info("🛑 Stopping Entertainment streaming...")
            self.streaming.stop_stream()
            logger.info("✅ Entertainment streaming stopped")
        except Exception as e:
            logger.error(f"Error stopping streaming: {e}")
    
    def print_config(self):
        """Print OpenRGB configuration instructions."""
        print("\n" + "="*70)
        print("🎉 E1.31 Bridge Started (Entertainment API - Ultra Low Latency)!")
        print("="*70)
        print(f"\nBridge: {self.bridge_name}")
        print(f"Entertainment Area: {self.area_name}")
        print(f"Number of lights: {self.num_lights}")
        print("\n📋 Configuration for OpenRGB:")
        print("-" * 70)
        print(f"\n  Name: Hue {self.area_name}")
        print(f"  IP (Unicast): (leave empty for multicast)")
        print(f"  Start Universe: {self.universe}")
        print(f"  Start Channel: 1")
        print(f"  Number of LEDs: {self.num_lights}")
        print(f"  Type: Single")
        print(f"  RGB Order: RGB")
        
        print("\n" + "-" * 70)
        print("\n🔧 How to add in OpenRGB:")
        print("  1. Open OpenRGB")
        print("  2. Go to Settings → E1.31 Devices")
        print("  3. Click 'Add'")
        print(f"  4. Name: Hue {self.area_name}")
        print(f"  5. IP (Unicast): leave EMPTY (use multicast)")
        print(f"  6. Start Universe: {self.universe}")
        print(f"  7. Start Channel: 1")
        print(f"  8. Number of LEDs: {self.num_lights}")
        print(f"  9. Type: Single")
        print(f"  10. RGB Order: RGB")
        print("  11. Click 'Save'")
        print("  12. IMPORTANT: Close OpenRGB completely and restart it")
        print("\n⚡ Using Entertainment API - zero lag, same as Hue Sync!")
        print("="*70 + "\n")
    
    def run(self):
        """Keep the bridge running."""
        self.start_streaming()
        self.print_config()
        logger.info("Bridge running... Press Ctrl+C to stop.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("\n🛑 Bridge stopped by user.")
            self.stop_streaming()
            logger.info("Stopping E1.31 receiver...")
            self.receiver.stop()
            logger.info("✅ Clean shutdown complete.")


# --------------------------------- Main ----------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Hue to E1.31 Bridge (Entertainment API)")
    ap.add_argument("--ip", help="Bridge IP (optional; overrides saved/discovered)")
    ap.add_argument("--app-key", help="Hue app key; overrides saved/env")
    ap.add_argument("--reset", action="store_true", help="Ignore saved config")
    
    # Pairing
    ap.add_argument("--re-pair", action="store_true", 
                    help="Re-pair with bridge to get Entertainment clientkey (press button!)")
    
    # Listing
    ap.add_argument("--list", action="store_true", help="List entertainment areas and exit")
    
    # Entertainment area selection
    ap.add_argument("--area", help="Entertainment area name to use")
    
    # E1.31 options
    ap.add_argument("--universe", type=int, default=1, help="E1.31 universe number (default: 1)")
    
    # Verbosity
    ap.add_argument("-v", "--verbose", action="count", default=0, 
                    help="Increase verbosity (-v: INFO, -vv: DEBUG)")

    args = ap.parse_args()
    
    # Set logging level
    if args.verbose == 0:
        logger.setLevel(logging.WARNING)
    elif args.verbose == 1:
        logger.setLevel(logging.INFO)
    elif args.verbose >= 2:
        logger.setLevel(logging.DEBUG)

    cfg = {} if args.reset else load_config()

    # Bridge IP resolution
    bridge_ip = args.ip or cfg.get("bridge_ip") or discover_bridge_ip()
    if not bridge_ip:
        logger.error("Could not discover the Hue Bridge IP. Pass --ip explicitly.")
        return 2
    
    logger.info(f"Bridge IP: {bridge_ip}")

    if cfg.get("bridge_ip") != bridge_ip:
        cfg["bridge_ip"] = bridge_ip
        cfg["updated_at"] = datetime.utcnow().isoformat() + "Z"

    # Handle re-pairing
    try:
        app_key, clientkey, cfg = ensure_app_key(bridge_ip, args.app_key, cfg, force_repair=args.re_pair)
        logger.debug(f"Username: {app_key[:8]}...")
        logger.debug(f"Clientkey: {clientkey[:8]}...")
    except Exception as e:
        logger.error(str(e))
        return 3

    # Save config after successful pairing
    save_config(cfg)
    
    # If we just re-paired, show success and exit
    if args.re_pair:
        print("✅ Re-pairing complete! Credentials saved.")
        print("   You can now use --list or --area to start the bridge.\n")
        return 0

    # Get bridge info for Entertainment API
    logger.info("Getting bridge information...")
    bridge_info = get_bridge_info(bridge_ip, app_key)
    
    logger.info(f"Bridge ID: {bridge_info['id']}")
    logger.info(f"Bridge Name: {bridge_info['name']}")

    # Create bridge object for Entertainment API
    try:
        bridge = create_bridge(
            identification=bridge_info["id"],
            rid=bridge_info["rid"],
            ip_address=bridge_ip,
            swversion=bridge_info["swversion"],
            username=app_key,
            hue_app_id=bridge_info.get("hue_app_id", ""),
            clientkey=clientkey,
            name=bridge_info["name"]
        )
        logger.info("✅ Bridge object created")
    except Exception as e:
        logger.error(f"Failed to create bridge object: {e}")
        return 4

    # List entertainment areas or run bridge
    if args.list or not args.area:
        try:
            entertainment_service = Entertainment(bridge)
            entertainment_configs = entertainment_service.get_entertainment_configs()
            
            # Get human-readable names
            area_names = get_entertainment_area_names(bridge_ip, app_key)
            
        except Exception as e:
            logger.error(f"Failed to get entertainment configurations: {e}")
            logger.error("Make sure you have created an Entertainment Area in the Hue app.")
            return 5
        
        print("\n📋 Entertainment Areas:")
        print("=" * 70)
        if not entertainment_configs:
            print("❌ No entertainment areas found!")
            print("\nℹ️  To create one:")
            print("  1. Open the Hue app")
            print("  2. Go to Settings → Entertainment areas")
            print("  3. Create a new area and add your lights")
            return 6
        
        for idx, (area_id, config) in enumerate(entertainment_configs.items()):
            # Get the human-readable name
            area_name = area_names.get(area_id, area_id)
            
            print(f"\n  [{idx+1}] {area_name}")
            print(f"      ID: {area_id}")
            print(f"      Lights: {len(config.channels)}")
            if hasattr(config, 'status'):
                status_str = str(config.status).replace("StatusTypes.", "")
                print(f"      Status: {status_str}")
        
        print("\n" + "=" * 70)
        if not args.list:
            print("\n⚠️  Use --area \"Area Name\" (or ID) to select an area")
        return 0

    # Start bridge
    logger.info(f"Starting Entertainment bridge for area '{args.area}'")
    
    try:
        bridge_instance = HueEntertainmentBridge(bridge, args.area, args.universe)
        bridge_instance.run()
    except Exception as e:
        logger.error(f"Failed to start bridge: {e}", exc_info=True)
        return 7
    
    return 0


if __name__ == "__main__":
    sys.exit(main())