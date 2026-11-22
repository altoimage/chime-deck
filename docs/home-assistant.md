# ChimeDeck in Home Assistant

ChimeDeck publishes one Home Assistant device per instance: a set of chime
buttons and one writable **selection switch per tile shown on the panel**.
Home Assistant is where you decide what a selection *means*; ChimeDeck itself
only records which tile is highlighted and plays the buttons you press.

This page assumes ChimeDeck is already installed and connected to your broker —
see [installing.md](installing.md) if it is not. The MQTT identifiers and the
`mqtt.*` settings live there too.

---

## What Home Assistant sees

The device is named after your unit (**Settings → Device name**) and groups
every entity ChimeDeck owns. There are two kinds:

| Entity | What it is |
| --- | --- |
| `button.<device>_<button>` | A chime button. Pressing it plays that button exactly as a dashboard tap does. These are hidden from the main dashboard (`entity_category: config`), so add them to a card or use them from an automation. |
| `switch.chime_deck_<slug>` | One selection switch per tile, named "`<tile> selected`". `on` means that tile is highlighted on the panel right now. |

**The switch mirrors the highlighted tile.** Tap a tile on the panel and Home
Assistant's switch turns on within a round trip; turn the switch on in Home
Assistant and the panel highlights the tile. More than one tile can be selected
at once — the service tracks a set — and an automation is free to act on
several. Treat the switches as intent, not as a radio group.

**The app records intent only and never presses a button itself.** Selecting a
tile does not chime anything: the policy that says "when the dryer is selected,
play the dryer-done chime and clear the tile" is an automation that lives in
Home Assistant. That is deliberate — the panel is a remote control, not a
rules engine.

The slug comes from the tile's display **name**, not its entity id. A tile
called "Dryer" is `switch.chime_deck_dryer`, and "Washing Machine" is
`switch.chime_deck_washing_machine`. If two tiles have the same name, the lower
entity id keeps the bare slug and the others take `_2`, `_3`, and so on.

---

## The dryer workflow, end to end

This is the whole point of the writable switch. When a load finishes:

1. You tap the **Dryer** tile on the panel. Its switch turns `on` in Home
   Assistant.
2. The automation below presses `button.chimedeck_dryer_done`, so the chime
   plays through ChimeDeck.
3. The same automation turns `switch.chime_deck_dryer` off, so the tile clears
   on the panel.

There is no doorbell-side logic in that loop: no rule on the Pi, no special
"dryer" button, nothing to keep in sync. The trigger is the tile's **switch
turning on**, not a time or a sensor state — the tile selection *is* the event.

```yaml
- id: dryer_done_chime
  alias: Dryer done chime
  description: Play the dryer chime and clear the selected tile.
  mode: single
  triggers:
    - trigger: state
      entity_id: switch.chime_deck_dryer
      to: "on"
  actions:
    - action: button.press
      target:
        entity_id: button.chimedeck_dryer_done
    - action: switch.turn_off
      target:
        entity_id: switch.chime_deck_dryer
```

Paste it into an automation in the Home Assistant UI (or your
`automations.yaml`), then check **Developer tools → States** to confirm the
entity ids match yours. `button.chimedeck_dryer_done` is the entity id Home
Assistant gave the button whose name is "Dryer Done"; `switch.chime_deck_dryer`
is pinned by ChimeDeck's discovery payload and does not depend on the device
name.

The order matters: press the chime **first**, then clear the tile. Clearing the
switch publishes `off`, which this trigger (`to: "on"`) ignores, so the
automation cannot re-trigger itself and loop.

---

## Commands are live; retained replays are refused

Home Assistant writes `on` or `off` to
`chime_deck/<instance>/selection/<slug>/set`. ChimeDeck accepts only a **live**
command:

- A **retained** command is refused. A broker keeps the last retained value and
  replays it to every new subscriber, so after a reconnect or a Home Assistant
  restart an old `on` could otherwise re-arm a tile days after the load
  finished. ChimeDeck treats the broker's RETAIN flag as "this is a replay of
  an old intent", ignores it, and republishes the current state so the switch
  snaps back.
- A command for a tile that is **no longer displayed** is refused and answered
  with `off`. Home Assistant may still have the switch while the panel has
  already dropped the tile; the answer tells it the truth instead of leaving it
  optimistic.
- A payload that is not exactly `on` or `off` is refused and answered with the
  current state. `ON`, `on ` (with a trailing space) and anything else are not
  command values.
- A command that repeats the current value is a **confirm**: ChimeDeck
  republishes the value so a lost packet is repaired, but does not treat it as
  a change, so you do not get a second chime.

Every refusal is a corrective republish, never silence. If a switch seems to
"snap back", that is usually this behaviour doing its job: something sent a
retained value, or a command for a tile the panel no longer shows.

---

## The selection does not survive a restart

**Every restart starts with nothing selected, and every switch is republished
as `off`.** The selection is volatile by design: a power blip has to disable
the automations that depend on a selection, not leave one armed forever. There
is no database column, no setting and no export/import path for it. If you need
a selection to survive a reboot, store it in Home Assistant (an `input_boolean`
or an `input_select`) and drive the ChimeDeck switch from that.

After a reconnect ChimeDeck republishes every switch's state as well, so Home
Assistant converges on the truth even if it was offline when a tile changed.

---

## Upgrading from the read-only selection sensors

Older builds published a **read-only** `binary_sensor.chime_deck_<slug>` per
tile. The current build publishes a writable `switch.chime_deck_<slug>` instead.

**Entity ids moved from `binary_sensor.chime_deck_<slug>` to
`switch.chime_deck_<slug>`.** Existing automations that reference the old
`binary_sensor` ids must be updated: a reference to a removed entity simply
never fires, with no error you would notice. Rename the entity in each
automation's trigger, action or template.

The old read-only entities clear themselves: on every connect ChimeDeck blanks
the old discovery config with a retained empty payload before it announces the
switch, so there is no dead `binary_sensor` left beside it and no broker
surgery is needed. The `unique_id` (`chime_deck_<instance>_<slug>_selected`)
and the retained state topic (`chime_deck/<instance>/selection/<slug>`) are
unchanged, so only the entity's *domain* moves — and, as always, every switch
starts from `off` when ChimeDeck starts, because the selection itself is never
persisted.

---

## Troubleshooting

**The switch is missing from Home Assistant.** Check **Settings → Devices &
services → MQTT** for the ChimeDeck device. If it is not there, MQTT discovery
is disabled or the discovery prefix differs; see the `mqtt.discovery_prefix`
setting in [installing.md](installing.md). The toggle still works from the
panel, because the panel does not need discovery — only Home Assistant does.

**The switch exists but the automation does nothing.** Open **Developer tools →
States** and type the entity id: `switch.chime_deck_dryer`, not
`binary_sensor.chime_deck_dryer` and not the tile's underlying Home Assistant
entity (`lock.front_door`). A stale entity id in an automation fails silently.

**The switch snaps back immediately.** Something is publishing a retained
command, or writing to a slug the panel no longer displays. Look for an
automation, script or MQTT publish that writes `on` to
`chime_deck/<instance>/selection/<slug>/set` with `retain: true`, and turn the
retain flag off. ChimeDeck's own log says `refused the retained command` or
`refused the command … no displayed tile has that selection slug` at INFO.

**The switch turns on and then off by itself, but no chime plays.** The trigger
fired twice, or a second automation owns the same switch. Check the automation
trace in Home Assistant; ChimeDeck only ever mirrors the value the last writer
sent.

**A tile was renamed and its switch disappeared.** Renaming a tile moves its
slug, so the entity id changes with it. Update the automation to the new id;
the old topics were blanked and its retained state cleared.

Still stuck? The install, broker and service questions are in
[installing.md](installing.md); the panel-side display issues are in
[kiosk.md](kiosk.md).
