# Putting ESPHome on BrizLabs string lights, over the air

BrizLabs "RY" string-light controllers can be re-flashed with ESPHome without opening the case
and without a serial cable. The original firmware has an update command, and `brizflash.py` uses it.

It takes about 15 minutes. If you can type a command and set up a smart plug with your phone,
you can do this.

1. `brizflash.py` puts a small setup firmware on the lights over WiFi (2 minutes).
2. The lights open their own setup hotspot. You join it with your phone and pick their WiFi.
3. Home Assistant finds them, and you add the BrizLabs light settings there.

## What you need

- A BrizLabs controller on its original firmware, plugged in, a few metres from the computer.
- A computer (Linux, macOS or Windows) with Bluetooth and Python 3.10 or newer, connected to a
  2.4 GHz WiFi network. Your home WiFi is usually fine. It is only used while flashing, so it
  does not have to be the network the lights end up on.
- A phone.
- Home Assistant with the ESPHome add-on, version 2026.11 or newer, for step 5.

If your WiFi is 5 GHz-only, WPA3-only, or a guest network that keeps devices apart, turn on your
phone's hotspot and connect the computer to that instead. On an iPhone, switch on "Maximize
Compatibility" so the hotspot uses 2.4 GHz.

## Step 1. Get the script ready

Download [`brizflash.py`](https://raw.githubusercontent.com/TFenby/esp32c2-26mhz/main/brizflash/brizflash.py)
(right-click the link, "Save link as") into a folder, open a terminal in that folder, and run:

```sh
python3 -m venv .venv
.venv/bin/pip install bleak
```

On Windows:

```powershell
py -m venv .venv
.venv\Scripts\pip install bleak
```

If you use [uv](https://docs.astral.sh/uv/), skip this step and start the script with
`uv run brizflash.py` in step 3.

## Step 2. Prepare the controller

1. Plug the controller in.
2. If the lights were ever set up in the BrizLabs phone app, their Bluetooth is off. Unplug the
   controller and plug it back in three times within 15 seconds. The status LED turns yellow and
   Bluetooth is back on.
3. Close the BrizLabs app on your phone. The controller talks to one Bluetooth device at a time,
   and the app holds on to it.
4. Turn Bluetooth on on the computer.

## Step 3. Flash

```sh
.venv/bin/python brizflash.py
```

On Windows: `.venv\Scripts\python brizflash.py`

The script asks for the name and password of the WiFi your computer is on. The password is not
shown while you type. Then it finds the controller and lets you blink the lights:

```
Connected. Before anything is changed, check it is the right string of lights.
Enter = toggle the lights   y = these are the lights, flash them   n = stop:
```

Press Enter a few times. If your lights switch on and off, type `y` and press Enter. If they do
not, type `n`: nothing has been changed. If several controllers are in range, the script lists
them first and asks which one to try.

Windows may ask whether Python is allowed to accept network connections. Allow it.

The rest runs by itself and takes about two minutes:

```
controller accepted the WiFi settings and is rebooting to join
waiting for the controller to join WiFi and connect to this computer (up to 2 min)
controller online: product 4fQzienJGITTlz, device rn01...
sent the update command, image at http://192.168.1.23:8080/firmware.bin
controller is not ready yet (it refuses updates for 10 s); waiting 11 s, then sending again
controller is downloading the image
downloading 50%  downloading
burning 100%  writing flash
done 100%  upgrade successful
SUCCESS: the controller flashed the new firmware and is rebooting into it.
```

The "not ready yet" line is normal.

## Step 4. Give the lights their WiFi

After SUCCESS the script prints the name of the lights' setup hotspot, for example
`esphome-web-26mhz-192d38`. It appears after about 30 seconds.

1. On your phone, join that network. It has no password.
2. A setup page opens. If it does not, open `http://192.168.4.1` in the phone's browser.
3. Pick the 2.4 GHz WiFi the lights should live on, enter its password, and save.

The hotspot disappears and the lights join your WiFi.

## Step 5. Turn them into BrizLabs lights

The lights now run a blank ESPHome. To add the light itself:

1. In Home Assistant, open the ESPHome add-on (ESPHome Builder). A device called
   `esphome-web-26mhz-xxxxxx` is listed with an Adopt button. Click it, give the lights a name,
   and click Adopt.
2. Click Edit on the adopted device and paste this at the end of the file:

   ```yaml
   external_components:
     - source: github://TFenby/esphome@add-brizlabs-string
       components: [brizlabs_string]

   # 24 V output stage for the string, on while any LED is lit.
   power_supply:
     - id: string_24v
       pin: GPIO3
       keep_on_time: 0s

   light:
     - platform: brizlabs_string
       name: Lights
       pin: 0
       power_supply: string_24v
       gamma_correct: 1.0
       restore_mode: RESTORE_DEFAULT_ON
       effects:
         - pulse:
         - random:
         - strobe:
         - flicker:
         - addressable_rainbow:
             width: 6
         - addressable_color_wipe:
         - addressable_scan:
         - addressable_twinkle:
         - addressable_random_twinkle:
         - addressable_fireworks:
         - addressable_flicker:
   ```

3. Click Save, then Install, then Wirelessly. After a few minutes of compiling, the lights show
   up in Home Assistant with on/off, colour, brightness and effects.

You are done. Later updates go through ESPHome, and you do not need `brizflash.py` again.

## If something goes wrong

A failed flash does no damage. The controller keeps its original firmware until the new one has
been written and checked, so you can fix the cause and run the script again.

| What you see | What to do |
|---|---|
| `download failed` | The computer has no internet. Download the `.ota.bin` from the [Releases](../../../releases) page another way and run `brizflash.py that-file.ota.bin`. |
| `no BrizLabs controller found over Bluetooth` | Do step 2 again: the phone app is still connected, or the controller's Bluetooth is off. |
| The lights do not react to Enter | It is a different controller. Answer `n` and try again, or unplug the other controllers. |
| `does not look like a home-network address (VPN active?)` | Turn the VPN off while flashing. |
| `the controller never connected to our broker` | Usually a typo in the WiFi password, or a network the controller cannot use (see "What you need"). Reset the controller with three quick power-ups and run the script again. |
| `download timeout` or `download stalled` | A firewall on the computer blocked the download, or the WiFi dropped. Allow Python through the firewall and run the script again. The controller reconnects by itself. |
| No `esphome-web-26mhz-...` network appears | Wait a minute. If it still does not appear, the controller has gone back to its original firmware (status LED yellow). Run the script again. |
| The setup page saved, but the lights never show up | The WiFi password was wrong, or the router uses mixed WPA2/WPA3. The setup hotspot comes back after about 90 seconds. For a mixed router, set it to WPA2 during setup, or use a phone hotspot until step 5 is done. |

The script has a few options for unusual setups (several controllers, several network
addresses). Run it with `--help` to see them.

There is no download of the original firmware anywhere. Flashing keeps it on the controller only
until the first ESPHome install in step 5. If you want to be able to go back, dump the flash over
serial before you start.

## Using your own firmware

`brizflash.py firmware.ota.bin` flashes any ESP32-C2 app image instead of the setup firmware.
Use `firmware.ota.bin`. The script refuses `firmware.factory.bin`. Your firmware needs WiFi
settings or a setup hotspot of its own, or the lights cannot be reached afterwards.

## How it works

The controller's original firmware can update itself from a web address, but only once it is on
WiFi. So the script does three things:

1. Over Bluetooth, it tells the controller to join your WiFi and to treat this computer as its
   update server.
2. When the controller connects, the script sends it the update command with a link to the
   firmware, which the script serves from this computer.
3. The controller downloads the image, checks it, writes it to the spare half of its flash, and
   reboots into it.

The setup firmware is this repo's ESPHome Web image, built for the board's 26 MHz crystal (see the
[main README](../README.md)). The script and both of its servers stop when flashing ends.
