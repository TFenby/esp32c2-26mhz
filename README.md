# ESPHome Web firmware for 26 MHz ESP32-C2 boards

The [esphome/firmware](https://github.com/esphome/firmware) repo publishes a ready-made
"ESPHome Web" bootstrap firmware per chip: it brings a blank device onto Wi-Fi (captive portal
or Improv) and lets the ESPHome Builder adopt it, after which the real configuration is
installed over the air. Its `esp32c2` build assumes a **40 MHz** crystal, because that is
ESP-IDF's default and the ESP32-C2 has no crystal auto-detection (only
[`XTAL_FREQ_26` or `XTAL_FREQ_40`](https://github.com/espressif/esp-idf/blob/v5.4.2/components/esp_hw_support/port/esp32c2/Kconfig.xtal)).
Many ESP32-C2 modules, including Espressif's own ESP8684-MINI-1 and ESP8684-WROOM-0xC, have a
**26 MHz** crystal. On those a 40 MHz build flashes fine and then mis-clocks the CPU, the UART
(the log is gibberish at 115200 baud) and the radio, so it never joins Wi-Fi. ESPHome only
[warns about the mismatch during a serial upload](https://github.com/esphome/esphome/pull/14582).

This repo is the upstream `esp32c2` config plus two lines:

```yaml
esp32:
  variant: esp32c2
  framework:
    type: esp-idf
    sdkconfig_options:
      CONFIG_XTAL_FREQ_26: y
      CONFIG_ESP_WIFI_ENABLE_WPA3_SAE: n
```

The second line builds without WPA3-SAE, which fails to associate with some WPA2/WPA3
transition-mode access points; the device joins those as WPA2 instead. Tasmota and OpenBeken
build their ESP32 firmware the same way. A WPA3-only network needs a build without that line.

Everything else (generator script, lint, CI, release layout) mirrors esphome/firmware so the
change can be offered upstream as a new `esp32c2-26mhz` entry.

## BrizLabs string lights

This firmware was built to get ESPHome onto BrizLabs "RY" string-light controllers, which have a
26 MHz ESP8684. [`brizflash/`](brizflash/) holds a script that installs it on them over the air
through the original firmware's own update command, with a step-by-step guide.

## Files

| Path | What |
|---|---|
| `esphome-web/esp32c2-26mhz.factory.yaml` | the bootstrap firmware config (captive portal, Improv serial, dashboard adoption) |
| `esphome-web/esp32c2-26mhz.yaml` | the config the ESPHome Builder imports when you adopt the device |
| `scripts/generate_esphome_web_configs.py` | upstream's generator with the `esp32c2-26mhz` entry; both YAML files are generated from it, never edited by hand |
| `brizflash/` | over-the-air installer and guide for BrizLabs string lights |
| `.github/workflows/build.yml` | builds the firmware with ESPHome's reusable workflow on pull requests, on demand, and on a release |
| `.github/workflows/check-generated-configs.yml`, `yaml-lint.yml` | upstream's checks, unchanged |

## Getting the firmware

Download from the [Releases](../../releases) page:

| Asset | Use |
|---|---|
| `esphome-web-26mhz-esp32c2.factory.bin` | full image for a serial flash: `esptool --chip esp32c2 write-flash 0x0 esphome-web-26mhz-esp32c2.factory.bin` |
| `esphome-web-26mhz-esp32c2.ota.bin` | app-only image for an over-the-air install onto a device whose current firmware can take one |
| `esphome-web-26mhz.manifest.json` | [ESP Web Tools](https://esphome.github.io/esp-web-tools/) manifest |

The files are named `esphome-web-26mhz-…` rather than upstream's `esphome-web-esp32c2.…` so the
two cannot be confused and so both could live in one combined release.

After flashing, the device starts an access point named `esphome-web-26mhz-XXXXXX`. Join it
from a phone, enter your Wi-Fi in the captive portal, and the device appears in the ESPHome
Builder under "Discovered" with an **Adopt** button. Adopt it, replace the imported config with
the one for your hardware, and install over the air.

## Building

Locally, with ESPHome 2026.4.0 or newer:

```sh
python3 scripts/generate_esphome_web_configs.py   # after editing the generator
esphome compile esphome-web/esp32c2-26mhz.factory.yaml
```

In CI, `build.yml` calls `esphome/workflows/.github/workflows/build.yml` (the same pinned
revision as upstream) and, on a published release, `upload-to-gh-release.yml` attaches the
binaries, manifest and checksums to that release. Upstream additionally uploads to Cloudflare
R2 and promotes to a production channel; those jobs need Cloudflare secrets and are left out.

To cut a release: create a GitHub release with a tag (upstream uses `YY.M.N`, for example
`26.9.2`); the build fills in `project.version` from the tag and uploads the assets.

## Upstreaming

The change for esphome/firmware is the `esp32c2-26mhz` entry and the two small generator
features it uses (`sdkconfig_options` under `framework`, and a per-platform `name`), plus one
line in `build.yml`. The `IMPORT_REPO` and `PROJECT_NAME` constants take upstream's values there
(`esphome/firmware`, `esphome.web`).
