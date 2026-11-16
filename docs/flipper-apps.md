# Flipper Zero apps: installed set

The seven apps MC approved on 2026-10-31, built from source and installed on 2026-11-02 to the
Flipper "Lle0zik" (official firmware 1.5.1, API 88.2), attached to nomad-b at `/dev/ttyACM0`.

Source of truth: the official catalog,
[flipperdevices/flipper-application-catalog](https://github.com/flipperdevices/flipper-application-catalog)
at `b18e8c4bae9eb670940064f11c20e6187b00bab2` (2026-11-01). Each app's
`applications/<Category>/<id>/manifest.yml` pins the source repo, commit and subdir used below.
Item 6 is two catalog apps (`sub_analyzer` and `subghz_raw_edit`), so both are installed: eight
`.fap` files in all.

| App | Catalog id | Category | Source repo (subdir) | Pinned commit | SDK | Installed path | Launch-verified |
|---|---|---|---|---|---|---|---|
| ProtoView | `protoview` | Sub-GHz | [xMasterX/all-the-plugins](https://github.com/xMasterX/all-the-plugins) (`base_pack/protoview`) | `1066d17ad1e8fbf75cea5c6612d79ba539ec8a33` | 1.5.1 (API 88.2) | `/ext/apps/Sub-GHz/protoview.fap` | yes |
| Spectrum Analyzer | `spectrum_analyzer` | Sub-GHz | [xMasterX/all-the-plugins](https://github.com/xMasterX/all-the-plugins) (`base_pack/spectrum_analyzer`) | `1066d17ad1e8fbf75cea5c6612d79ba539ec8a33` | 1.5.1 (API 88.2) | `/ext/apps/Sub-GHz/spectrum_analyzer.fap` | yes |
| POCSAG Pager | `pocsag_pager` | Sub-GHz | [xMasterX/all-the-plugins](https://github.com/xMasterX/all-the-plugins) (`base_pack/pocsag_pager`) | `1066d17ad1e8fbf75cea5c6612d79ba539ec8a33` | 1.5.1 (API 88.2) | `/ext/apps/Sub-GHz/pocsag_pager.fap` | yes |
| Weather Station | `weather_station` | Sub-GHz | [flipperdevices/flipperzero-good-faps](https://github.com/flipperdevices/flipperzero-good-faps) (`weather_station`) | `e1311a6da20491b20c32bfc7c00c6b6da5c7a9a7` | 1.5.1 (API 88.2) | `/ext/apps/Sub-GHz/weather_station.fap` | yes |
| TPMS Reader | `tpms` | Sub-GHz | [xMasterX/all-the-plugins](https://github.com/xMasterX/all-the-plugins) (`apps_source_code/tpms_receiver`) | `a2004a3539c50ced20f6f24e61ba17c1dd86460b` | 1.5.1 (API 88.2) | `/ext/apps/Sub-GHz/tpms.fap` | yes |
| Sub Analyzer | `sub_analyzer` | Sub-GHz | [xMasterX/all-the-plugins](https://github.com/xMasterX/all-the-plugins) (`apps_source_code/sub_analyzer`) | `a2004a3539c50ced20f6f24e61ba17c1dd86460b` | 1.5.1 (API 88.2) | `/ext/apps/Sub-GHz/sub_analyzer.fap` | yes |
| Sub-GHz RAW Edit | `subghz_raw_edit` | Sub-GHz | [Lechnio/SubGHz-RAW-Edit](https://github.com/Lechnio/SubGHz-RAW-Edit) (repo root) | `49a12e37a8c940a1665477bb41eb25d21bc82721` | 1.5.1 (API 88.2) | `/ext/apps/Sub-GHz/subghz_raw_edit.fap` | yes |
| i2c Tools | `i2ctools` | GPIO | [NaejEL/flipperzero-i2ctools](https://github.com/NaejEL/flipperzero-i2ctools) (repo root) | `51e0d451e25fab4b229ff020747c939e8f27d786` | 1.5.1 (API 88.2) | `/ext/apps/GPIO/i2ctools.fap` | yes |

## How it was built and installed

- **Build:** on nomad-b in a scratch dir (`/tmp/fap-build`, removed afterwards). Each repo was
  fetched at its pinned commit and built with `ufbt` (release channel, SDK 1.5.1, hw target f7).
  All eight builds exited 0 and `APPCHK` reported `Target: 7, API: 88.2` for each.
- **Install:** `~/.ufbt/current/scripts/storage.py -p /dev/ttyACM0 send <fap> /ext/apps/<Category>/<id>.fap`,
  one file at a time, after `fuser` showed nothing holding `/dev/ttyACM0`.
- **Not touched:** firmware, the SD card's other files, the Wi-Fi devboard, the kiosk, the
  `sigint101` service and `meshtasticd`. USB over-current count in `dmesg` was 188 before and 188
  after; the Flipper did not re-enumerate during the run.

## Verification (observed over the Flipper CLI)

- **`storage list`:** all eight files are present at the paths above.
- **`storage md5`:** each on-card file's MD5 matches the MD5 of the build output.

  | File | MD5 |
  |---|---|
  | `protoview.fap` | `9730ed4270195816f9b1d0699023f6ec` |
  | `spectrum_analyzer.fap` | `5ff58322035409d30775421ab78edbd8` |
  | `pocsag_pager.fap` | `a69b7bdd6fb977130a7da4a58f132c6a` |
  | `weather_station.fap` | `9e94027f8764b20d22b50977387db491` |
  | `tpms.fap` | `54a4cf0eac3154dd12df0cc2ade30f68` |
  | `sub_analyzer.fap` | `7361d28c3ee81d8b3e774d93f6ab97a4` |
  | `subghz_raw_edit.fap` | `4ed7a81684adc20e04505372aca41938` |
  | `i2ctools.fap` | `8018b4f322ac09ce1bffecdb35c690be` |

- **Launch:** each app was started with `loader open <path>`. About four seconds later
  `loader info` reported it by name ("ProtoView", "Spectrum Analyzer", "POCSAG Pager",
  "Weather Station", "TPMS Reader", "Sub Analyzer", "Sub-GHz RAW Edit", "i2c Tools") as running.
  It was then backed out with Back key events sent through `input send`. `loader close` refuses
  these apps ("has to be closed manually"), and ProtoView only quits on a long Back. The run ended
  with `loader info` reporting "No application is running", so the Flipper was on its main screen.
  "Launch-verified" means the loader ran the app. Nobody inspected each app's screen beyond that.

## Notes

- **Five apps were already on the card**, as older builds: ProtoView, Spectrum Analyzer, POCSAG
  Pager, Weather Station and i2c Tools. Their `.fapmeta` targeted API 87.1, from before the
  1.4.3 to 1.5.1 firmware update. The 1.5.1 builds replaced them in place. Before the overwrite,
  the old files were copied off the card with `storage.py receive`. The copies sit in a
  session scratch directory on the droplet, not in this repo.
- **I2C Tools:** the catalog also lists `GPIO/i2c_tools_cli`, a fork by @Tristus1er. It was not
  installed. The approved app is the original `i2ctools`.
- **TPMS:** the catalog id is `tpms`. The app's own name is "TPMS Reader".
- No forks were substituted. Every app came from the official catalog at its pinned commit.
