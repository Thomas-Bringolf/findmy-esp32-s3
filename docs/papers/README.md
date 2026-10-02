# 📄 `docs/papers/` — the paper behind this protocol

## [Who Can Find My Devices? Security and Privacy of Apple's Crowd-Sourced Bluetooth Location Tracking System](https://doi.org/10.2478/popets-2021-0045)

| | |
|---|---|
| **Authors** | Alexander Heinrich, Milan Stute, Tim Kornhuber, Matthias Hollick |
| **Affiliation** | Secure Mobile Networking Lab (SEEMOO), TU Darmstadt |
| **Venue** | *Proceedings on Privacy Enhancing Technologies* (PoPETs) 2021(3):227–245 |
| **DOI** | [10.2478/popets-2021-0045](https://doi.org/10.2478/popets-2021-0045) |
| **Open access page** | [petsymposium.org/popets/2021/popets-2021-0045.php](https://petsymposium.org/popets/2021/popets-2021-0045.php) |
| **Local copy** | [`popets-2021-0045.pdf`](popets-2021-0045.pdf) |

This is **the** reverse-engineering paper for Apple's Offline Finding (OF)
protocol. The frame layout, the P-224 key chain and the location-report
encryption that this repository implements (`ESP32/main/`,
`Scripts/findmy-toolbox.py`) come from it — it is also the paper the
[OpenHaystack](https://github.com/seemoo-lab/openhaystack) project cites.

### License of this PDF

> Copyright in PoPETs articles are held by their authors. This article is
> published under a **Creative Commons Attribution-NonCommercial-NoDerivs 3.0**
> license (CC BY-NC-ND 3.0).

So the file is redistributed here **unmodified**, with full attribution and
the DOI above, for non-commercial use only. It is *not* covered by this
repository's [AGPL-3.0](../../LICENSE) / [MIT](../../LICENSE-MIT) licences,
and no derivative version of it may be published.

### See also

* *DEMO: OpenHaystack: A Framework for Tracking Personal Bluetooth Devices
  via Apple's Massive Find My Network* — WiSec '21,
  [10.1145/3448300.3468251](https://doi.org/10.1145/3448300.3468251)
* Apple's own description of the cryptography:
  [Find My security](https://support.apple.com/guide/security/find-my-security-sec6cbc80fd0/web)
