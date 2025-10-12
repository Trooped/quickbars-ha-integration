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

To configure the QuickBars for Home Assistant TV app from the integration:

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

## Actions

### Action: `quickbars.quickbar_toggle` {% icon "mdi:television-guide" %}

Open or close a QuickBar overlay by its alias.

- **Fields**
  - `device_id` *(optional)* — Target a specific QuickBars device. If omitted, broadcasts to all connected QuickBars devices.
  - `alias` *(required)* — The QuickBar alias defined in the TV app.

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

### Action: `quickbars.camera_toggle` {% icon "mdi:video-wireless" %}

Show a camera overlay on the TV.
The camera MUST be imported into QuickBars and have an **MJPEG** stream URL.
Provide either an alias (as known to QuickBars) or select a Home Assistant camera entity.


- **Fields**

  - `device_id` *(optional)* - Target a specific QuickBars device. If omitted, broadcasts to all connected QuickBars devices.
  - `camera_alias` *(optional)* - Camera Alias as configured in QuickBars TV app (in the Manage Saved Entities screen). You must use this or camera_entity.
  - `camera_entity` *(optional)* - Home Assistant camera that maps to a camera imported to QuickBars. You must use this or camera_alias.
  - `size` *(optional)* - small, medium, large. If not specified, uses the size configured to the camera entity on the TV app. You can use this or size_px.
  - `size_px` *(optional)* - Custom size, for example {"w":640,"h":360}. Use instead of size.
  - `position` *(optional)* - top_left, top_right, bottom_left, bottom_right. If not specified, uses the position configured to the camera entity on the TV app.
  - `show_title` *(optional)* - Show the camera's name on the stream. If not specified, uses the show_title configured to the camera entity on the TV app.
  - `auto_hide` *(optional)* - Seconds before auto-hide; 0 = never (need to toggle manually). If not specified, uses the auto_hide configured to the camera entity on the TV app.

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

### Action: `quickbars.notify` {% icon "mdi:message-arrow-right" %}
Display a rich TV notification with optional icon, image, sound, action buttons and more.

- **Fields**

  - `device_id` *(optional)* - Target a specific QuickBars device. If omitted, broadcasts to all connected QuickBars devices.
  - `title` *(optional)* - Add a title on top of the notification.
  - `message` *(required)* - The main message of the notification.
  - `actions` *(optional)* - List of {id, label} for action buttons. You can create automations based on the id.
  - `length` *(optional, default 6s)* - How long to show the notification (seconds).
  - `color` *(optional, default #8cd0e6)* - The background color of the notification in RGB format.
  - `transparency` *(optional, default 0)* - The transparency of the background. 0 for opaque, 100 for fully transparent.
  - `actions` *(optional)* - List of {id, label} for action buttons. You can create automations based on the id.
  - `mdi_icon` *(optional)* - Pick an MDI icon (e.g., mdi:bell). The integration will embed the SVG and show it near the title.
  - `image` *(optional, choose one)* - |
         Provide ONE of these examples:
          - url: "https://example.com/pic.jpg"
          - path: "folder/file.jpg"      (relative to /config/www → served at /local/folder/file.jpg)
          - media_id: "media-source://media_source/local/folder/file.jpg"  (from My media)
  - `image_media` *(optional)* - Select an image directly from Home Assistant's Media Browser (My media). This is a shortcut for the **image** field above. Use only one of the approaches.
  - `sound` *(optional, choose one)* - |
  Provide ONE of thes examples:
          - url: "https://example.com/file.mp3"
          - path: "chimes/ding.mp3"      (relative to /config/www → /local/chimes/ding.mp3)
          - media_id: "media-source://media_source/local/chimes/ding.mp3"  (from My media)
  - `sound_media` *(optional)* - Select an audio file directly from Home Assistant's Media Browser (My media). This is a shortcut for the **sound** field above. Use only one of the approaches.
  - `sound_volume_percent` *(optional, default 100%)* -         0–200%. 100% = system volume; >100% uses post-mix boost (may clip/distort).
  - `interrupt` *(optional)* - Hide any existing notification, and show this one immediately.
  - `position` *(optional, default top_right)* - top_left, top_right, bottom_left, bottom_right.

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

## Events
When a user selects an action button on a TV notification, QuickBars sends that action back to Home Assistant as an event.
Use the Events trigger in an automation and listen for the QuickBars action event name used by the integration.

{% details "Example: react to a TV action selection" %}

```yaml
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
```

## Examples
Doorbell: pop-up camera and actionable notification

```yaml
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
```

## Data updates
QuickBars uses local push interactions (no polling) and on-demand requests during the Options flow (for example, exporting saved entities, updating QuickBars configuration on the TV).

## Known limitations
The app must be open (foreground) when configuring the app using the Options Menu.

The integration does not create entities; it exposes service actions and emits/handles events.

Advanced UI features (more than 1 QuickBar, advance layouts such as grid option) require QuickBars Plus in the TV app.

## Troubleshooting
Can’t set up the device (“TV not reachable”)

#### Symptom: The setup form shows “TV not reachable”.

#### Resolution

1. Ensure the TV is powered on and the QuickBars TV app is open (foreground).
2. Try exiting the app and re-opening it.
3. Confirm the TV and Home Assistant are on the same network.


"""""""""
For discovery, make sure Zeroconf/mDNS works on your LAN.

If using a Home Assistant URL/token:

Verify the long-lived access token is valid.
"""""""""""""

### Camera/Notification don't appear on the TV after sending them

#### Symptom: Events are sent using the integration, but don't appear on the TV.

#### Resolution

1. Make sure "persistent background connection" is enabled on your QuickBars TV app in the settings.
2. for Cameras - verify the camera has an MJPEG stream, and it's imported to the TV app. 

## Removing the integration
This integration follows standard integration removal.

{% include integrations/remove_device_service.md %}

After deleting the integration in Home Assistant, open the QuickBars TV app and clear the Home Assistant integration pairing in the app's settings as well. It's required if you want to re-pair to HA again.