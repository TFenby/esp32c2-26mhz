#!/usr/bin/env python3
"""
Script to generate ESPHome Web configuration files from templates.

This script generates both regular and factory configurations for different
ESP32 variants, ESP8266, Raspberry Pi Pico W / Pico 2 W, and LibreTiny (BK72xx,
LN882H, RTL87xx) platforms.

Requires Python 3.13+ for modern typing features (TypedDict, Literal, etc.)

Usage:
    python3 scripts/generate_esphome_web_configs.py
"""

import sys
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

# Check Python version
if sys.version_info < (3, 13):
    print("Error: This script requires Python 3.13 or higher")
    print(f"Current version: {sys.version}")
    sys.exit(1)

# Configuration constants
MIN_VERSION: str = "2026.4.0"
# GitHub repo that the factory configs import their adoptable config from
IMPORT_REPO: str = "TFenby/esp32c2-26mhz"
# ESPHome project identifier (<vendor>.<project>) stamped into the factory firmware
PROJECT_NAME: str = "tfenby.esphome-web-26mhz"

# Type definitions for platform configuration
PlatformKey = Literal["esp32", "esp8266", "rp2040", "bk72xx", "ln882x", "rtl87xx"]


class FrameworkConfig(TypedDict):
    type: Literal["arduino", "esp-idf"]
    # Pins a framework version other than ESPHome's recommended one
    version: NotRequired[str]
    # Extra ESP-IDF sdkconfig lines the hardware needs (e.g. crystal frequency)
    sdkconfig_options: NotRequired[dict[str, str]]


class BoardConfig(TypedDict, total=False):
    # ESP32 platforms use variant
    variant: Literal[
        "esp32",
        "esp32c2",
        "esp32c3",
        "esp32c5",
        "esp32c6",
        "esp32c61",
        "esp32s2",
        "esp32s3",
        "esp32s31",
    ]
    framework: FrameworkConfig
    # ESP8266, RP2040 and LibreTiny use board
    board: Literal[
        "esp01_1m",
        "rpipicow",
        "rpipico2w",
        "generic-bk7231n-qfn32-tuya",
        "generic-bk7231t-qfn32-tuya",
        "generic-bk7238-tuya",
        "generic-bk7252",
        "generic-ln882h-tuya",
        "generic-rtl8710bn-2mb-788k",
        "generic-rtl8720cf-2mb-992k",
    ]


class PlatformConfig(TypedDict):
    board_config: BoardConfig
    # Device name, and so the prefix of the built files; the default "esphome-web"
    # yields esphome-web-<variant>.*, which a second config for the same variant
    # would collide with
    name: NotRequired[str]
    has_bluetooth: bool
    has_captive_portal: bool
    platform_key: NotRequired[PlatformKey]
    # Overrides MIN_VERSION for a platform that needs a newer ESPHome
    min_version: NotRequired[str]


# Platform configurations
PLATFORMS: dict[str, PlatformConfig] = {
    "esp32c2-26mhz": {
        # The esp32c2 config for modules with a 26 MHz crystal (Espressif's own
        # ESP8684-MINI-1 and ESP8684-WROOM-0xC, and products built on them).
        # ESP-IDF cannot detect the crystal on the C2 and defaults to 40 MHz; a
        # 40 MHz build mis-clocks the CPU, UART and radio on a 26 MHz board.
        "board_config": {
            "variant": "esp32c2",
            "framework": {
                "type": "esp-idf",
                "sdkconfig_options": {"CONFIG_XTAL_FREQ_26": "y"},
            },
        },
        # Not enough RAM for esp32_improv alongside Wi-Fi
        "has_bluetooth": False,
        "has_captive_portal": True,
        "name": "esphome-web-26mhz",
    },
}


def render_mapping(mapping: dict, indent: int) -> str:
    """Render a nested dict as YAML, two spaces per level."""
    out: str = ""
    for key, value in mapping.items():
        if isinstance(value, dict):
            out += f"{'  ' * indent}{key}:\n" + render_mapping(value, indent + 1)
        else:
            out += f"{'  ' * indent}{key}: {value}\n"
    return out


def create_base_config(platform_name: str, platform_config: PlatformConfig) -> str:
    """Create the base configuration for a platform."""
    # Build platform section
    platform_key: str = platform_config.get("platform_key", "esp32")
    platform_section: str = f"{platform_key}:\n"

    board_config: BoardConfig = platform_config["board_config"]
    platform_section += render_mapping(board_config, indent=1)

    # Combine all sections
    config: str = f"""esphome:
  name: {platform_config.get('name', 'esphome-web')}
  friendly_name: ESPHome Web
  min_version: {platform_config.get('min_version', MIN_VERSION)}
  name_add_mac_suffix: true

{platform_section}
# Enable logging
logger:

# Enable Home Assistant API
api:

# Allow Over-The-Air updates
ota:
  - platform: esphome

wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
"""

    return config


def create_factory_config(platform_name: str, platform_config: PlatformConfig) -> str:
    """Create the factory configuration for a platform."""
    # Build platform section
    platform_key: str = platform_config.get("platform_key", "esp32")
    platform_section: str = f"{platform_key}:\n"

    board_config: BoardConfig = platform_config["board_config"]
    platform_section += render_mapping(board_config, indent=1)

    # Build the factory config with full configuration
    config: str = f"""esphome:
  name: {platform_config.get('name', 'esphome-web')}
  friendly_name: ESPHome Web
  min_version: {platform_config.get('min_version', MIN_VERSION)}
  name_add_mac_suffix: true
  project:
    name: {PROJECT_NAME}
    version: dev

{platform_section}
# Enable logging
logger:

# Enable Home Assistant API
api:

# Allow Over-The-Air updates
ota:
  - platform: esphome

# Allow provisioning Wi-Fi via serial
improv_serial:

wifi:
  # Set up a wifi access point
  ap: {{}}"""

    # Add captive portal for platforms that support it
    has_captive_portal: bool = platform_config.get("has_captive_portal", True)
    if has_captive_portal:
        config += """

# In combination with the `ap` this allows the user
# to provision wifi credentials to the device via WiFi AP.
captive_portal:"""

    config += f"""

# Allows taking control of the device in the ESPHome Builder/Dashboard
dashboard_import:
  package_import_url: github://{IMPORT_REPO}/esphome-web/{platform_name}.yaml@main
  import_full_config: true"""

    # Add Bluetooth support for platforms that support it
    has_bluetooth: bool = platform_config["has_bluetooth"]
    if has_bluetooth:
        config += """

# Sets up Bluetooth LE (Only on ESP32) to allow the user
# to provision wifi credentials to the device.
esp32_improv:
  authorizer: none"""

    config += "\n"
    return config


def write_yaml_file(filepath: str | Path, config: str) -> None:
    """Write configuration to YAML file with proper formatting."""
    # Ensure directory exists using pathlib
    Path(filepath).parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w") as f:
        f.write(config)

    print(f"Generated: {filepath}")


def main() -> None:
    """Generate all ESPHome Web configuration files."""
    script_dir: Path = Path(__file__).parent
    repo_root: Path = script_dir.parent
    esphome_web_dir: Path = repo_root / "esphome-web"

    print("Generating ESPHome Web configuration files...")
    print(f"Output directory: {esphome_web_dir}")

    for platform_name, platform_config in PLATFORMS.items():
        # Generate base configuration
        base_config: str = create_base_config(platform_name, platform_config)
        base_filepath: Path = esphome_web_dir / f"{platform_name}.yaml"
        write_yaml_file(base_filepath, base_config)

        # Generate factory configuration
        factory_config: str = create_factory_config(platform_name, platform_config)
        factory_filepath: Path = esphome_web_dir / f"{platform_name}.factory.yaml"
        write_yaml_file(factory_filepath, factory_config)

    print("\nGeneration complete!")


if __name__ == "__main__":
    main()
