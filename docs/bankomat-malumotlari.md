# Bankomat ma’lumoti qanday keladi

Manba — `monitoring.btech.uz`. Server yoniq turganida yig‘uvchi har soatda barcha bankomatlarni so‘raydi va javobni `atm_monitor` sxemasiga yozadi. Eski qator o‘chirilmaydi: har so‘rov yangi tarix.

Hozir bazada 473 ta bankomat, 5 ta muvaffaqiyatli so‘rov (20-oktabr 15:17 dan 18:19 gacha har soat, keyin server o‘chiq turgan, 21-oktabr 14:21 da yana bitta so‘rov).

Quyidagi misol shu bazadan olingan, o‘ylab topilmagan.

## Misol: TID 544830

| | |
|---|---|
| Ichki id | 18906161 |
| TID | 544830 |
| Seriya | 13-54544830 |
| Model | NCR 6622, faqat naqd beradi (Cash Out) |
| Joy | Олтинтепа МФЙ, Паркент кўчаси 227-uy, bank binosi 24/7 |
| Filial raqami | 00440 |
| Birinchi ko‘rilgan | 2026-10-20 15:17 |
| Oxirgi ko‘rilgan | 2026-10-21 14:21 |

BTech bitta bankomatni bitta katta JSON qilib yuboradi. Yig‘uvchi uni uch joyga ajratadi va JSON ning o‘zini ham saqlaydi.

```text
BTech JSON
    │
    ├─ atms                  pasport (manzil, model). Har so‘rovda yangilanadi, tarix emas.
    ├─ atm_snapshots         shu soatdagi holat. Har so‘rov yangi qator.
    ├─ cassette_snapshots    shu soatdagi har bir kasseta.
    ├─ turnover_snapshots    BTech hisoblagan xarajat tezligi.
    └─ raw_json              kelgan JSON ning hammasi, maydon qolib ketmasligi uchun.
```

## 1. Pasport — `atms`

Bu bankomatning o‘zi. `id` bo‘yicha yangilanadi, shuning uchun 5 ta so‘rovda ham bitta qator.

JSON dagi joylar:

| JSON | Ustun | Shu bankomatda |
|---|---|---|
| `tid` | `tid` | 544830 |
| `serial` | `serial` | 13-54544830 |
| `card.address` | `address` | Олтинтепа МФЙ Паркент кўчаси 227 уй |
| `card.place` | `place` | Банк биноси 24/7 |
| `card.branchNumber` | `branch_number` | 00440 |
| `card.extraAttrs.mfo` | `mfo` | 00424 |
| `card.gpsCoords` | `latitude`, `longitude` | 41.317199, 69.337831 |
| `model.name` / `model.vendor.name` / `model.type.name` | `model_name`, `vendor_name`, `type_name` | 6622 / NCR / Cash Out |
| `status` | `status` | production |

## 2. Holat — `atm_snapshots`

Har soat alohida qator. Shu bankomatda 5 ta qator bor. Oxirgi so‘rov (21-oktabr 14:21) dan:

| JSON | Ustun | Qiymat | Ma’nosi |
|---|---|---|---|
| `agentStatus.status` | `agent_status` | online | monitoring agenti tarmoqda |
| `agentStatus.lastOnline` | `agent_last_online` | 14:20:44 | agent oxirgi marta ko‘ringan vaqt |
| `state.serviceStatus` | `service_status` | InService | mijozga xizmat ochiq |
| `state.appStatus` | `app_status` | InService | dastur ishlayapti |
| `state.appConnStatus` | `app_conn_status` | Online | ilova ulangan |
| `state.platformStatus` | `platform_status` | Ready | platforma tayyor |
| `state.supSwitchStatus` | `sup_switch_status` | Normal | nazorat kaliti odatiy holatda |
| `deviceStatus.dispenser` | `dispenser_status` | ONLINE | pul beradigan modul ishlayapti |
| `deviceStatus.cardReader` | `card_reader_status` | BUSY | karta o‘quvchi band (mijoz turibdi yoki seans ochiq) |
| `deviceStatus.epp` | `epp_status` | BUSY | PIN klaviatura band |
| `deviceStatus.printer` | `printer_status` | ONLINE | chek printeri ishlayapti |
| `deviceStatus.acceptor` | `acceptor_status` | bo‘sh | bu model pul qabul qilmaydi |
| `cdmRemainingAmount.totalUzs` | `cdm_total_uzs` | 495 110 000 | kassetadagi so‘m |
| `lastTransactionTimestamp.cashOut` | `last_transaction_cash_out` | 14:04:59 | oxirgi naqd yechish |
| `lastTransactionTimestamp.cashIn` | `last_transaction_cash_in` | bo‘sh | pul qabul qilish bo‘lmagan |

Qolgan to‘rt soat ham shu ustunlarga yozilgan. Qoldiq shunday tushgan:

| So‘rov vaqti | Qoldiq, so‘m | Oldingisidan farq |
|---|---:|---:|
| 20-oktabr 15:17 | 562 930 000 | — |
| 20-oktabr 16:17 | 562 230 000 | −700 000 |
| 20-oktabr 17:18 | 561 290 000 | −940 000 |
| 20-oktabr 18:19 | 561 040 000 | −250 000 |
| 21-oktabr 14:21 | 495 110 000 | −65 930 000 |

Oxirgi farq bir soatlik yechim emas: orada server o‘chiq turgan, shu tun va ertalabki soatlar yozilmagan. Pasayishning o‘zi saqlanadi, lekin qaysi soatda yechilgani shu tanaffus ichida ko‘rinmaydi.

## 3. Kassetalar — `cassette_snapshots`

`cdmCassetteStatusBrief` ro‘yxatidagi har bir kasseta alohida qator. Oxirgi so‘rovda 8 ta:

| № | Turi | Holat | Soni | Nominal | Hisob |
|---|---|---|---:|---:|---:|
| 1 | pul kassetasi | LOW | 10 | 200 000 | 2 000 000 |
| 2 | pul kassetasi | INOP | 1 937 | 200 000 | 387 400 000 |
| 3 | pul kassetasi | INOP | 1 742 | 50 000 | 87 100 000 |
| 4 | pul kassetasi | LOW | 1 861 | 10 000 | 18 610 000 |
| 5–7 | pul kassetasi | MISSING | 0 | — | o‘rnatilmagan |
| 8 | brak kassetasi | OK | 152 | — | qaytarilgan kupuralar |

1–4 kassetalarning yig‘indisi 495 110 000. Bu `cdm_total_uzs` bilan bir xil: BTech qoldiqni shu kassetalardan hisoblaydi. 2- va 3-kassetalar nosoz (`INOP`), lekin ichidagi pul qoldiqqa kiradi.

`LOW` — kupura kam, `MISSING` — kasseta yo‘q, `OK` — brak kassetasi hali to‘lmagan.

## 4. Tezlik — `turnover_snapshots`

`turnoverTotal` dan. Oxirgi so‘rovda bitta qator, chunki bankomat faqat so‘m beradi:

| Maydon | Qiymat | Ma’nosi |
|---|---:|---|
| `balance` | 495 110 000 | shu paytdagi qoldiq |
| `dispense_amount_rate` | 3 093 333 so‘m/soat | BTech o‘zi hisoblagan berish tezligi |
| `forecast_hours` | 160,06 soat | shu tezlikda pul taxminan shuncha soatga yetadi |

Bu bizning modelimiz emas. Bu raqamni BTech yuboradi, biz uni so‘rov bilan birga saqlaymiz.

## 5. To‘liq JSON — `raw_json`

Yuqoridagi ustunlardan tashqari kelgan hamma narsa shu yerda: agent versiyasi (`01.08.17`), dastlabki yuklama (`cdmInitialAmount.totalUzs` = 920 000 000), karta oynasi, model foto manzili. Yangi maydon qo‘shilsa ham, u avval shu JSON ichida qoladi.

## Prognoz shu bankomat uchun

Prognoz alohida yoziladi: `forecast_runs`, `atm_forecast_summary`, `atm_forecasts`.

Hozir o‘qitilgan model yo‘q. Kuniga kamida 12 soatlik so‘rov va shunday 14 kun kerak. Hozir eng uzun kun 4 soat. Shuning uchun ikkala hisob ham `fallback`, usul `btech`:

```text
sutkalik yechim = oxirgi qoldiq / BTech forecast_hours × 24
```

21-oktabr 14:21 dagi hisob 18:19 dagi suratdan olingan (qoldiq 561 040 000), chunki yangi so‘rov prognozdan bir soniya keyin yozilgan. Natija:

| | |
|---|---|
| Usul | btech |
| O‘z tarixidan kunlar | 0 |
| Sutkalik yechim | 56 680 249 so‘m |
| 7 kunlik yechim | 362 843 826 so‘m |
| 7 kundan keyin qolishi kutilgan qoldiq | 198 196 174 so‘m |
| Pul tugash vaqti | yozilmagan — 7 kun ichida nolga tushmaydi |
| Tavsiya etilgan yuklama | 0 |

Yetti kunning har biri bir xil 56,7 mln. Bu tekis chiziq, chunki zaxira usul har kunga bitta tezlikni qo‘yadi. XGBoost ishlaganda kunlar har xil bo‘ladi: hafta kuni, oylik va bayramlar hisobga olinadi.
