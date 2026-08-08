# Hardware

| Part | Model |
| --- | --- |
| Base | Fanatec CSL Elite Wheel Base — USB `0eb7:0e03`, `bcdDevice 0693` |
| Wheel | Fanatec F1 Carbon |
| Pedals | Fanatec CSL Elite, load-cell brake |
| Pedal wiring | Throttle / brake / clutch / handbrake each on a 6-pin RJ-type jack on a shared controller board |

The rim and pedals report *through* the base; there are no separate USB devices.
The base's boot display reads `693` → `22` → `---`; `693` is the firmware
version, matching `bcdDevice 0693`.

## Device nodes

Nodes move on replug (`.0107` → `.0109`), so always resolve through
`/dev/input/by-id/usb-Fanatec_*`, never a hardcoded `hidrawN` or `eventN`.
`hid_layout.find_nodes()` is the single place that knows the names.

## Pedal jacks

Jacks are 6-pin (small RJ-type), not 3-pin. Outermost pins (0 ↔ 5) read a stable
3.3 V on throttle, brake and clutch, so the sensor supply reaches all three. The
handbrake jack has no 3.3 V there — different pinout or unpopulated, and
irrelevant here.

Pedal channels are pot dividers off that 3.3 V supply, which is why a voltage
readout is meaningful for them. Steering is an encoder, so it is not.

A pedal sweeping only ~1/3 of a pot's electrical arc is normal by design. The
requirement is only that at rest the wiper sits just off one end stop and at full
travel has not run past the other. Mark pot body → shaft and photograph it before
removing one.
