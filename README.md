# Israel Electric Corporation (IEC) Custom Component

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

![Project Maintenance][maintenance-shield]
[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]


_Integration to integrate with [IEC API][iec]._

**This integration will set up the following platforms.**

![Example Image][exampleimg]


Platform | Description
-- | --
`sensor` | Show info from IEC invoice and meters.

## Installation

Automatic (HACS):
1. Add this path to HACS: `https://github.com/GuyKh/iec-custom-component`
2. Install through HACS

Manual:
1. Using the tool of choice open the directory (folder) for your HA configuration (where you find `configuration.yaml`).
1. If you do not have a `custom_components` directory (folder) there, you need to create it.
1. In the `custom_components` directory (folder) create a new folder called `iec`.
1. Download _all_ the files from the `custom_components/iec/` directory (folder) in this repository.
1. Place the files you downloaded in the new directory (folder) you created.
1. Restart Home Assistant
1. In the HA UI go to "Configuration" -> "Integrations" click "+" and search for "IEC"

## Configuration is done in the UI

## Logs
To view logs in debug add this to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    ...
    custom_components.iec: debug
    iec_api: debug
```

<!---->

## Frequently Asked Questions

#### How often is the data fetched?
<br> The component currently fetches data from IEC every hour, but IEC doesn't update the data very often and they don't really have a consistant behavior, it could arrive with 2 days delay.

#### Can this component be used even if I'm using a private electric company? 
<br> **Yes**. However, the only difference is that in the Invoices you'll see balance = `0` since you're paying to the private company

#### Should I enter my configuration credentials every time?
<br> **No**. You should do it only once when configuring the component.


#### I have a "dumb" meter, would this component still work for me.
<br> **Yes, But...**. you would only get a subset of sensors, based on your Invoice information.

<!---->

## Examples of usage.
You can head to [Examples page](examples.md) to see examples of this components used in cards in HomeAssistant

<!---->

## Using Postman Collection for calling the IEC API

If you want to use Postman to get the API calls - please read the [Postman Collections Guide](https://github.com/GuyKh/py-iec-api/blob/main/POSTMAN.md)

<!---->

## Contributions are welcome!

If you want to contribute to this please read the [Contribution guidelines](CONTRIBUTING.md)

***

[iec]: https://www.iec.co.il/home
[buymecoffee]: https://www.buymeacoffee.com/guykh
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[commits-shield]: https://img.shields.io/github/commit-activity/y/guykh/iec-custom-component.svg?style=for-the-badge
[commits]: https://github.com/guykh/iec-custom-component/commits/main
[exampleimg]: example.png
[license-shield]: https://img.shields.io/github/license/guykh/iec-custom-component.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-Guy%20Khmelnitsky%20%40GuyKh-blue.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/guykh/iec-custom-component.svg?style=for-the-badge
[releases]: https://github.com/guykh/iec-custom-component/releases
