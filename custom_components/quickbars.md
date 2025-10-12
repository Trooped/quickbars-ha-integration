---
title: QuickBars
description: Control the QuickBars for Home Assistant Android TV app from Home Assistant configure entities, QuickBars and send camera PiPs, QuickBars, and rich notifications to the TV.
ha_release: 2025.11
ha_iot_class: Local Polling
ha_codeowners:
  - '@Trooped'
ha_domain: quickbars
ha_integration_type: service
related:
  - url: https://developers.home-assistant.io/docs/documenting/standards
    title: Documentation standard
  - url: https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/
    title: Integration Quality Scale - Rules
  - docs: /docs/glossary/
    title: Glossary
  - docs: /docs/tools/quick-bar/#my-links
    title: My link
---

The **QuickBars** {% term integration %} connects Home Assistant to the **QuickBars for Home Assistant** Android / Google TV app. The app enables on-screen **QuickBars overlays** which lets you control Entities and view their states Quickly, without disrupting the viewing experience. 

Use case: Setup **QuickBars for Home Assistant** app initially by sending URL and Long-Lived Token using the integration. Configure your entities (add or remove entities from the app), configure existing QuickBars or create new ones. Send a Home Assistant **event** to toggle a QuickBar, a camera PiP, or a rich notification, with optional actions.

{% tip %}
QuickBars works entirely on your local network (no cloud). Its {% term integration %} exposes **service actions** and emits events. No entities are created.
{% endtip %}

## Supported devices

- Android TV / Google TV devices with Android 9 or higher.

## Unsupported devices

- TVs/streamers which are *not* Android TV.

## Prerequisites

1. Install the **QuickBars for Home Assistant** app on your TV/streamer.
2. Ensure your Android TV device and Home Assistant are on the **same LAN**.
3. Open the QuickBars TV app. Keep it on screen for first-time pairing (you’ll enter a pairing code).
4. If prompted, provide your Home Assistant URL and a long-lived access token.  
   {% note %}Avoid `localhost`. Use an IP or hostname reachable from the TV.{% endnote %}

{% include integrations/config_flow.md %}

Go to {% my integrations title="**Settings** > **Devices & services**" %}, select **Add integration** {% icon "mdi:plus" %}, and search for **QuickBars**. Follow the on-screen instructions.

## Configuration options

After adding QuickBars, you can change options later:

- Go to {% my integrations title="**Settings** > **Devices & services**" %}.
- Select **QuickBars** > **Configure** {% icon "mdi:cog-outline" %}.

The Options flow provides:

{% configuration_basic %}
Add / remove saved entities:
  description: Pick which Home Assistant entities are “saved” (imported) to the QuickBars TV app.
Manage Save Entities:
  description: Configure your saved (imported) entities (right now only friendly name change is supported).
Manage QuickBars:
  description: Create or edit QuickBars (name, entities, position, grid layout, overlay options, custom colors, and domain auto-close rules).
{% endconfiguration_basic %}

## Supported functionality

This integration exposes **service actions** (no entities). You can call them from automations and scripts.

### Service: `quickbars.quickbar_toggle` {% icon "mdi:television-guide" %}

Open or close a QuickBar overlay by its alias.

- **Fields**
  - `device_id` *(optional)* — Target a specific QuickBars device. If omitted, broadcasts to all connected QuickBars devices.
  - `alias` *(required)* — The QuickBar alias defined in the TV app (for example, `living_room`).

**Example**

```yaml
# Good
action:
  - action: quickbars.quickbar_toggle
    data:
      alias: living_room
    target:
      device_id: 1234567890abcdef
```

### Service: `quickbars.camera_toggle` {% icon "mdi:video-wireless" %}

Show a camera as a picture-in-picture overlay on the TV. 

- **Fields**

  - `device_id` *(optional)* - Target a specific QuickBars device. If omitted, broadcasts to all connected QuickBars devices.
  - `camera_alias` *(optional) - Camera Alias as configured in QuickBars TV app (in the Manage Save Entities screen). You must use this or camera_entity.
  - `camera_entity` *(optional) - Home Assistant camera that maps to a camera imported to QuickBars. You must use this or camera_alias.
  - `position` *(optional) - top_left, top_right, bottom_left, bottom_right. If not specified, uses the position configured to the camera entity on the TV app.
  - `size` *(optional) - small, medium, large. If not specified, uses the size configured to the camera entity on the TV app. You can use this or size_px.
  - `size_px` *(optional) - Custom size, for example {"w": 640,"h": 360}. Use instead of size.
  - `auto_hide` *(optional) - Seconds before auto-hide; 0 = never. If not specified, uses the auto_hide configured to the camera entity on the TV app.
  - `show_title` *(optional) - Determines if you see the camera's name on the stream. If not specified, uses the show_title configured to the camera entity on the TV app.

**Example**

```yaml
# Good
action:
  - action: quickbars.camera_toggle
    data:
      camera_entity: camera.front_door
      position: bottom_left
      auto_hide: 10
    target:
      device_id: 1234567890abcdef
```

### Service: `quickbars.notify` {% icon "mdi:message-arrow-right" %}
Display a rich TV notification with optional icon, image, sound, and action buttons.

- **Fields**

  - `device_id` *(optional)* - Target a specific QuickBars device. If omitted, broadcasts to all connected QuickBars devices.
  - `title` *(optional)* - Add a title on top of the notification.
  - `message` *(required)* - The main message of the notification.
  - `actions` *(optional)* - List of {id, label} for action buttons. You can create automations based on the id.
  - `actions` *(optional)* - List of {id, label} for action buttons. You can create automations based on the id.
  - `color` *(optional, default #8cd0e6)* - The background color of the notification.
  - `transparency` *(optional, default 0)* - The transparency of the background. 0 for opaque, 100 for fully transparent.
  - `actions` *(optional)* - List of {id, label} for action buttons. You can create automations based on the id.
  - `mdi_icon` *(optional)* - Add an icon in the title row. For example, mdi:bell.
  - `image` *(optional, choose one)* - {"url": "https://..."} | {"path": "images/file.jpg"} (under /config/www → /local/...) | {"media_id": "media-source://..."}. 
  - `image_media` *(optional, choose one)* -



position (required) — top_left, top_right, bottom_left, bottom_right.

length (optional, default 6) — Seconds to display.


Image (optional; choose one) — {"url": "https://..."} | {"path": "images/file.jpg"} (under /config/www → /local/...) | {"media_id": "media-source://..."}

Sound (optional; choose one) — {"url": "https://..."} | {"path": "chimes/ding.mp3"} | {"media_id": "media-source://..."}

sound_volume_percent (optional, default 100) — 0–200% (values >100% apply post-mix boost)

**Example**

```yaml
Copy code
# Good
action:
  - action: quickbars.notify
    data:
      title: Package delivered
      mdi_icon: mdi:package-variant
      message: "Someone is at the door"
      actions:
        - id: open_door
          label: Open
        - id: dismiss
          label: Dismiss
      position: bottom_left
      image:
        path: images/doorbell.jpg
      sound:
        path: chimes/ding.mp3
      sound_volume_percent: 120
    target:
      device_id: 1234567890abcdef
```

{% important %}
Prefer service action targets (target.device_id, target.entity_id, or target.area_id) over putting identifiers inside data. This is the modern pattern and keeps automations consistent.
{% endimportant %}

Events
When a viewer selects an action button on a TV notification, QuickBars sends that action back to Home Assistant as an event.
Use the Events trigger in an automation and listen for the QuickBars action event name used by the integration. Filter on device_id if you target a single TV.

{% details "Example: react to a TV action selection" %}

yaml
Copy code
# Good
triggers:
  - trigger: event
    event_type: quickbars_action  # Replace with the exact event name you configured
    event_data:
      device_id: 1234567890abcdef
conditions: []
actions:
  - action: light.turn_on
    target:
      entity_id: light.entryway
{% enddetails %}

Examples
Doorbell: pop-up camera and actionable notification
yaml
Copy code
# Good
alias: "Doorbell on TV"
triggers:
  - trigger: state
    entity_id: binary_sensor.doorbell
    to: "on"
actions:
  - action: quickbars.camera_toggle
    target:
      device_id: abcdef123456
    data:
      camera_entity: camera.front_door
      position: top_right
      auto_hide: 8
  - action: quickbars.notify
    target:
      device_id: abcdef123456
    data:
      title: "Doorbell"
      message: "Someone is at the door"
      mdi_icon: mdi:doorbell
      actions:
        - id: open
          label: "Open"
        - id: ignore
          label: "Ignore"
      position: bottom_left
Data updates
QuickBars uses local push interactions (no polling) and on-demand requests during the Options flow (for example, exporting saved entities, updating QuickBars configuration on the TV).

Known limitations
The camera overlay requires an MJPEG stream configured in the QuickBars TV app.

The integration does not create entities; it exposes service actions and emits/handles events.

Advanced UI features (for example, some positions or grid layout) may require QuickBars Plus in the TV app.

Troubleshooting
Can’t set up the device (“TV not reachable”)
Symptom
The setup form shows “TV not reachable”.

Resolution

Ensure the TV is powered on and the QuickBars TV app is open.

Confirm the TV and Home Assistant are on the same network.

For discovery, make sure Zeroconf/mDNS works on your LAN.

If using a Home Assistant URL/token:

Use an IP or hostname reachable from the TV (avoid localhost).

Verify the long-lived access token is valid.

Camera overlay doesn’t appear
Verify the camera has an MJPEG stream and is imported into the QuickBars TV app.

If selecting by camera_entity, ensure it maps to a camera known to QuickBars.

Actions aren’t received in my automation
Verify the event type and (optionally) device_id in your trigger match the integration’s event payload.

Confirm the QuickBars TV app still has network access to Home Assistant.

Removing the integration
This integration follows standard removal.

{% include integrations/remove_device_service.md %}

After deleting the integration in Home Assistant, open the QuickBars TV app and remove the Home Assistant connection there as well.