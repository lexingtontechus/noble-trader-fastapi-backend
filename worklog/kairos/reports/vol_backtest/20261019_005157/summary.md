# Vol Forecast Backtest — Phase 0 (measured or nothing)

**Run:** 2026-10-19 00:52:27 · source: `yfinance(hourly)` · symbols: BTCUSD, XAGUSD, XAUUSD, USDJPY, USDCHF · horizons: [6, 24, 48, 72] · wall: 28.7s

Reference bar: **Log-HAR** (Brini 2026, arXiv 2607.05291 — only TTM beat Log-HAR, by 1.3–1.8%; TimesFM & Chronos-Bolt did not). Secondary null: naive. Loss: QLIKE. Directional accuracy vs the persistence rule. DM: Newey-West HAC (overlap-aware) + Holm. Per-symbol rows — not pooled.

## Acceptance criteria check (thesis §benchmark, corrected nulls)

| criterion | verdict per symbol (see dm_tests.csv for stats) |
|---|---|

## Metrics (per symbol × horizon × arm)

### horizon = 6 bars

| symbol   | arm      |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:---------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | ewma     | 698 |  0.1903 |    0.5228 |    0.6533 |                 0.3983 |       0.4527 |
| BTCUSD   | har      | 697 |  0.1960 |    0.4775 |    0.6743 |                 0.3983 |       0.7647 |
| BTCUSD   | har_ewma | 697 |  0.1845 |    0.4935 |    0.6643 |                 0.3983 |       0.6184 |
| BTCUSD   | naive    | 698 |  0.2561 |    0.5174 |    0.4871 |                 0.3983 |     nan      |
| USDCHF   | ewma     | 686 |  0.1757 |    0.4767 |    0.6676 |                 0.3586 |       0.4840 |
| USDCHF   | har      | 685 |  0.1529 |    0.4088 |    0.7182 |                 0.3586 |       0.7985 |
| USDCHF   | har_ewma | 685 |  0.1574 |    0.4373 |    0.7036 |                 0.3586 |       0.6642 |
| USDCHF   | naive    | 686 |  0.2917 |    0.5509 |    0.4898 |                 0.3586 |     nan      |
| USDJPY   | ewma     | 685 |  0.1663 |    0.4668 |    0.6818 |                 0.3898 |       0.5139 |
| USDJPY   | har      | 684 |  0.1648 |    0.4227 |    0.7105 |                 0.3898 |       0.7880 |
| USDJPY   | har_ewma | 684 |  0.1541 |    0.4370 |    0.6930 |                 0.3898 |       0.6433 |
| USDJPY   | naive    | 685 |  0.2830 |    0.5413 |    0.4847 |                 0.3898 |     nan      |
| XAGUSD   | ewma     | 542 |  0.1552 |    0.4717 |    0.7251 |                 0.3690 |       0.5166 |
| XAGUSD   | har      | 541 |  0.1508 |    0.4192 |    0.7431 |                 0.3690 |       0.7837 |
| XAGUSD   | har_ewma | 541 |  0.1438 |    0.4364 |    0.7486 |                 0.3690 |       0.6673 |
| XAGUSD   | naive    | 542 |  0.2968 |    0.5684 |    0.5258 |                 0.3690 |     nan      |
| XAUUSD   | ewma     | 542 |  0.1470 |    0.4616 |    0.6937 |                 0.3469 |       0.5111 |
| XAUUSD   | har      | 541 |  0.1459 |    0.4022 |    0.7523 |                 0.3469 |       0.7874 |
| XAUUSD   | har_ewma | 541 |  0.1379 |    0.4260 |    0.7320 |                 0.3469 |       0.6765 |
| XAUUSD   | naive    | 542 |  0.2521 |    0.5474 |    0.4926 |                 0.3469 |     nan      |

### horizon = 24 bars

| symbol   | arm      |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:---------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | ewma     | 697 |  0.1149 |    0.3807 |    0.6571 |                 0.3989 |       0.3185 |
| BTCUSD   | har      | 695 |  0.1018 |    0.3473 |    0.6892 |                 0.3989 |       0.7856 |
| BTCUSD   | har_ewma | 695 |  0.0990 |    0.3536 |    0.7022 |                 0.3989 |       0.6043 |
| BTCUSD   | naive    | 697 |  0.1564 |    0.4301 |    0.5222 |                 0.3989 |     nan      |
| USDCHF   | ewma     | 685 |  0.0871 |    0.3172 |    0.6175 |                 0.3504 |       0.3737 |
| USDCHF   | har      | 683 |  0.0701 |    0.2837 |    0.7013 |                 0.3504 |       0.7526 |
| USDCHF   | har_ewma | 683 |  0.0698 |    0.2861 |    0.6940 |                 0.3504 |       0.5988 |
| USDCHF   | naive    | 685 |  0.1022 |    0.3370 |    0.4672 |                 0.3504 |     nan      |
| USDJPY   | ewma     | 684 |  0.1170 |    0.3352 |    0.5921 |                 0.3699 |       0.3845 |
| USDJPY   | har      | 682 |  0.1067 |    0.3103 |    0.6833 |                 0.3699 |       0.7698 |
| USDJPY   | har_ewma | 682 |  0.1028 |    0.3096 |    0.6789 |                 0.3699 |       0.6378 |
| USDJPY   | naive    | 684 |  0.1374 |    0.3510 |    0.4751 |                 0.3699 |     nan      |
| XAGUSD   | ewma     | 541 |  0.0856 |    0.3258 |    0.6100 |                 0.3530 |       0.3567 |
| XAGUSD   | har      | 539 |  0.0699 |    0.2793 |    0.7087 |                 0.3530 |       0.7644 |
| XAGUSD   | har_ewma | 539 |  0.0698 |    0.2929 |    0.6827 |                 0.3530 |       0.5974 |
| XAGUSD   | naive    | 541 |  0.1013 |    0.3500 |    0.4769 |                 0.3530 |     nan      |
| XAUUSD   | ewma     | 541 |  0.0770 |    0.3016 |    0.6026 |                 0.3604 |       0.3771 |
| XAUUSD   | har      | 539 |  0.0645 |    0.2654 |    0.6957 |                 0.3604 |       0.7774 |
| XAUUSD   | har_ewma | 539 |  0.0643 |    0.2748 |    0.6753 |                 0.3604 |       0.6122 |
| XAUUSD   | naive    | 541 |  0.0866 |    0.3165 |    0.4954 |                 0.3604 |     nan      |

### horizon = 48 bars

| symbol   | arm      |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:---------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | ewma     | 696 |  0.1014 |    0.3497 |    0.5704 |                 0.4253 |       0.2328 |
| BTCUSD   | har      | 693 |  0.0712 |    0.2902 |    0.6724 |                 0.4253 |       0.7821 |
| BTCUSD   | har_ewma | 693 |  0.0720 |    0.2967 |    0.6825 |                 0.4253 |       0.5657 |
| BTCUSD   | naive    | 696 |  0.1021 |    0.3569 |    0.4971 |                 0.4253 |     nan      |
| USDCHF   | ewma     | 684 |  0.0744 |    0.2942 |    0.5658 |                 0.4357 |       0.2836 |
| USDCHF   | har      | 681 |  0.0560 |    0.2493 |    0.6931 |                 0.4357 |       0.7621 |
| USDCHF   | har_ewma | 681 |  0.0528 |    0.2515 |    0.6564 |                 0.4357 |       0.5800 |
| USDCHF   | naive    | 684 |  0.0724 |    0.2932 |    0.5088 |                 0.4357 |     nan      |
| USDJPY   | ewma     | 683 |  0.1169 |    0.3230 |    0.5783 |                 0.4202 |       0.2782 |
| USDJPY   | har      | 680 |  0.0972 |    0.2997 |    0.6779 |                 0.4202 |       0.7353 |
| USDJPY   | har_ewma | 680 |  0.0956 |    0.2950 |    0.6588 |                 0.4202 |       0.5853 |
| USDJPY   | naive    | 683 |  0.1208 |    0.3271 |    0.5081 |                 0.4202 |     nan      |
| XAGUSD   | ewma     | 540 |  0.0703 |    0.2909 |    0.5167 |                 0.3981 |       0.2630 |
| XAGUSD   | har      | 537 |  0.0540 |    0.2382 |    0.6741 |                 0.3981 |       0.7654 |
| XAGUSD   | har_ewma | 537 |  0.0515 |    0.2476 |    0.6108 |                 0.3981 |       0.5773 |
| XAGUSD   | naive    | 540 |  0.0638 |    0.2796 |    0.5000 |                 0.3981 |     nan      |
| XAUUSD   | ewma     | 540 |  0.0698 |    0.2796 |    0.5370 |                 0.4204 |       0.3000 |
| XAUUSD   | har      | 537 |  0.0547 |    0.2370 |    0.6741 |                 0.4204 |       0.7654 |
| XAUUSD   | har_ewma | 537 |  0.0544 |    0.2454 |    0.6443 |                 0.4204 |       0.5885 |
| XAUUSD   | naive    | 540 |  0.0649 |    0.2690 |    0.4907 |                 0.4204 |     nan      |

### horizon = 72 bars

| symbol   | arm      |   n |   qlike |   mae_log |   dir_acc |   dir_null_persistence |   coverage80 |
|:---------|:---------|----:|--------:|----------:|----------:|-----------------------:|-------------:|
| BTCUSD   | ewma     | 695 |  0.0957 |    0.3312 |    0.5957 |                 0.4734 |       0.1827 |
| BTCUSD   | har      | 691 |  0.0569 |    0.2596 |    0.6874 |                 0.4734 |       0.7598 |
| BTCUSD   | har_ewma | 691 |  0.0597 |    0.2671 |    0.6657 |                 0.4734 |       0.5427 |
| BTCUSD   | naive    | 695 |  0.0799 |    0.3115 |    0.4604 |                 0.4734 |     nan      |
| USDCHF   | ewma     | 683 |  0.0708 |    0.2868 |    0.5417 |                 0.4085 |       0.2387 |
| USDCHF   | har      | 679 |  0.0520 |    0.2325 |    0.6878 |                 0.4085 |       0.7452 |
| USDCHF   | har_ewma | 679 |  0.0456 |    0.2344 |    0.6657 |                 0.4085 |       0.5685 |
| USDCHF   | naive    | 683 |  0.0618 |    0.2730 |    0.5022 |                 0.4085 |     nan      |
| USDJPY   | ewma     | 682 |  0.1238 |    0.3289 |    0.5484 |                 0.4238 |       0.2302 |
| USDJPY   | har      | 678 |  0.0920 |    0.2893 |    0.7035 |                 0.4238 |       0.7212 |
| USDJPY   | har_ewma | 678 |  0.0935 |    0.2884 |    0.6696 |                 0.4238 |       0.5206 |
| USDJPY   | naive    | 682 |  0.1185 |    0.3160 |    0.5147 |                 0.4238 |     nan      |
| XAGUSD   | ewma     | 539 |  0.0645 |    0.2731 |    0.5046 |                 0.4508 |       0.2560 |
| XAGUSD   | har      | 535 |  0.0480 |    0.2184 |    0.6393 |                 0.4508 |       0.7458 |
| XAGUSD   | har_ewma | 535 |  0.0449 |    0.2232 |    0.6037 |                 0.4508 |       0.5738 |
| XAGUSD   | naive    | 539 |  0.0491 |    0.2397 |    0.5083 |                 0.4508 |     nan      |
| XAUUSD   | ewma     | 539 |  0.0687 |    0.2726 |    0.5139 |                 0.4527 |       0.2523 |
| XAUUSD   | har      | 535 |  0.0525 |    0.2251 |    0.6579 |                 0.4527 |       0.7701 |
| XAUUSD   | har_ewma | 535 |  0.0518 |    0.2318 |    0.6000 |                 0.4527 |       0.5551 |
| XAUUSD   | naive    | 539 |  0.0545 |    0.2421 |    0.5269 |                 0.4527 |     nan      |

### QLIKE winners (lower = better)

| symbol   |   horizon | best_arm   |   qlike |   har_qlike |
|:---------|----------:|:-----------|--------:|------------:|
| BTCUSD   |         6 | har_ewma   |  0.1845 |      0.1960 |
| BTCUSD   |        24 | har_ewma   |  0.0990 |      0.1018 |
| BTCUSD   |        48 | har        |  0.0712 |      0.0712 |
| BTCUSD   |        72 | har        |  0.0569 |      0.0569 |
| USDCHF   |         6 | har        |  0.1529 |      0.1529 |
| USDCHF   |        24 | har_ewma   |  0.0698 |      0.0701 |
| USDCHF   |        48 | har_ewma   |  0.0528 |      0.0560 |
| USDCHF   |        72 | har_ewma   |  0.0456 |      0.0520 |
| USDJPY   |         6 | har_ewma   |  0.1541 |      0.1648 |
| USDJPY   |        24 | har_ewma   |  0.1028 |      0.1067 |
| USDJPY   |        48 | har_ewma   |  0.0956 |      0.0972 |
| USDJPY   |        72 | har        |  0.0920 |      0.0920 |
| XAGUSD   |         6 | har_ewma   |  0.1438 |      0.1508 |
| XAGUSD   |        24 | har_ewma   |  0.0698 |      0.0699 |
| XAGUSD   |        48 | har_ewma   |  0.0515 |      0.0540 |
| XAGUSD   |        72 | har_ewma   |  0.0449 |      0.0480 |
| XAUUSD   |         6 | har_ewma   |  0.1379 |      0.1459 |
| XAUUSD   |        24 | har_ewma   |  0.0643 |      0.0645 |
| XAUUSD   |        48 | har_ewma   |  0.0544 |      0.0547 |
| XAUUSD   |        72 | har_ewma   |  0.0518 |      0.0525 |

## Diebold-Mariano (QLIKE, HAC + Holm)

#### vs `har`

| symbol   |   horizon | arm_a    | arm_b   |   n |   mean_loss_diff |   dm_stat |   p_value |   p_holm |   hac_lag | significant_holm   |
|:---------|----------:|:---------|:--------|----:|-----------------:|----------:|----------:|---------:|----------:|:-------------------|
| BTCUSD   |         6 | ewma     | har     | 697 |          -0.0058 |   -0.8961 |    0.3702 |   0.3702 |         0 | False              |
| BTCUSD   |         6 | har_ewma | har     | 697 |          -0.0114 |   -2.7388 |    0.0062 |   0.0123 |         0 | True               |
| BTCUSD   |         6 | naive    | har     | 697 |           0.0604 |    4.3449 |    0.0000 |   0.0000 |         0 | False              |
| BTCUSD   |        24 | ewma     | har     | 695 |           0.0131 |    2.6523 |    0.0080 |   0.0160 |         0 | False              |
| BTCUSD   |        24 | har_ewma | har     | 695 |          -0.0028 |   -0.9763 |    0.3289 |   0.3289 |         0 | False              |
| BTCUSD   |        24 | naive    | har     | 695 |           0.0547 |    6.5292 |    0.0000 |   0.0000 |         0 | False              |
| BTCUSD   |        48 | ewma     | har     | 693 |           0.0304 |    5.8900 |    0.0000 |   0.0000 |         1 | False              |
| BTCUSD   |        48 | har_ewma | har     | 693 |           0.0008 |    0.3294 |    0.7419 |   0.7419 |         1 | False              |
| BTCUSD   |        48 | naive    | har     | 693 |           0.0312 |    5.4588 |    0.0000 |   0.0000 |         1 | False              |
| BTCUSD   |        72 | ewma     | har     | 691 |           0.0391 |    6.7683 |    0.0000 |   0.0000 |         2 | False              |
| BTCUSD   |        72 | har_ewma | har     | 691 |           0.0028 |    1.1179 |    0.2636 |   0.2636 |         2 | False              |
| BTCUSD   |        72 | naive    | har     | 691 |           0.0233 |    4.1873 |    0.0000 |   0.0001 |         2 | False              |
| USDCHF   |         6 | ewma     | har     | 685 |           0.0221 |    3.8976 |    0.0001 |   0.0002 |         0 | False              |
| USDCHF   |         6 | har_ewma | har     | 685 |           0.0045 |    1.2612 |    0.2072 |   0.2072 |         0 | False              |
| USDCHF   |         6 | naive    | har     | 685 |           0.1390 |    8.3347 |    0.0000 |   0.0000 |         0 | False              |
| USDCHF   |        24 | ewma     | har     | 683 |           0.0168 |    3.8222 |    0.0001 |   0.0003 |         0 | False              |
| USDCHF   |        24 | har_ewma | har     | 683 |          -0.0002 |   -0.0971 |    0.9226 |   0.9226 |         0 | False              |
| USDCHF   |        24 | naive    | har     | 683 |           0.0316 |    5.5305 |    0.0000 |   0.0000 |         0 | False              |
| USDCHF   |        48 | ewma     | har     | 681 |           0.0183 |    2.9019 |    0.0037 |   0.0111 |         1 | False              |
| USDCHF   |        48 | har_ewma | har     | 681 |          -0.0033 |   -0.7162 |    0.4739 |   0.4739 |         1 | False              |
| USDCHF   |        48 | naive    | har     | 681 |           0.0160 |    2.5715 |    0.0101 |   0.0202 |         1 | False              |
| USDCHF   |        72 | ewma     | har     | 679 |           0.0188 |    2.0439 |    0.0410 |   0.1229 |         2 | False              |
| USDCHF   |        72 | har_ewma | har     | 679 |          -0.0064 |   -0.8325 |    0.4051 |   0.5725 |         2 | False              |
| USDCHF   |        72 | naive    | har     | 679 |           0.0094 |    1.0664 |    0.2862 |   0.5725 |         2 | False              |
| USDJPY   |         6 | ewma     | har     | 684 |          -0.0023 |   -0.3303 |    0.7412 |   0.7412 |         0 | False              |
| USDJPY   |         6 | har_ewma | har     | 684 |          -0.0108 |   -2.2847 |    0.0223 |   0.0447 |         0 | True               |
| USDJPY   |         6 | naive    | har     | 684 |           0.1149 |    6.9689 |    0.0000 |   0.0000 |         0 | False              |
| USDJPY   |        24 | ewma     | har     | 682 |           0.0093 |    1.6159 |    0.1061 |   0.2122 |         0 | False              |
| USDJPY   |        24 | har_ewma | har     | 682 |          -0.0039 |   -1.1363 |    0.2558 |   0.2558 |         0 | False              |
| USDJPY   |        24 | naive    | har     | 682 |           0.0297 |    4.7750 |    0.0000 |   0.0000 |         0 | False              |
| USDJPY   |        48 | ewma     | har     | 680 |           0.0195 |    3.1684 |    0.0015 |   0.0036 |         1 | False              |
| USDJPY   |        48 | har_ewma | har     | 680 |          -0.0016 |   -0.4642 |    0.6425 |   0.6425 |         1 | False              |
| USDJPY   |        48 | naive    | har     | 680 |           0.0230 |    3.2409 |    0.0012 |   0.0036 |         1 | False              |
| USDJPY   |        72 | ewma     | har     | 678 |           0.0319 |    3.5276 |    0.0004 |   0.0013 |         2 | False              |
| USDJPY   |        72 | har_ewma | har     | 678 |           0.0014 |    0.3340 |    0.7384 |   0.7384 |         2 | False              |
| USDJPY   |        72 | naive    | har     | 678 |           0.0263 |    2.5731 |    0.0101 |   0.0202 |         2 | False              |
| XAGUSD   |         6 | ewma     | har     | 541 |           0.0047 |    0.6588 |    0.5100 |   0.5100 |         0 | False              |
| XAGUSD   |         6 | har_ewma | har     | 541 |          -0.0069 |   -1.4943 |    0.1351 |   0.2702 |         0 | False              |
| XAGUSD   |         6 | naive    | har     | 541 |           0.1466 |    5.8210 |    0.0000 |   0.0000 |         0 | False              |
| XAGUSD   |        24 | ewma     | har     | 539 |           0.0160 |    2.7833 |    0.0054 |   0.0108 |         0 | False              |
| XAGUSD   |        24 | har_ewma | har     | 539 |          -0.0000 |   -0.0108 |    0.9914 |   0.9914 |         0 | False              |
| XAGUSD   |        24 | naive    | har     | 539 |           0.0318 |    4.7697 |    0.0000 |   0.0000 |         0 | False              |
| XAGUSD   |        48 | ewma     | har     | 537 |           0.0165 |    2.8616 |    0.0042 |   0.0126 |         1 | False              |
| XAGUSD   |        48 | har_ewma | har     | 537 |          -0.0025 |   -0.6583 |    0.5104 |   0.5104 |         1 | False              |
| XAGUSD   |        48 | naive    | har     | 537 |           0.0099 |    1.8722 |    0.0612 |   0.1224 |         1 | False              |
| XAGUSD   |        72 | ewma     | har     | 535 |           0.0168 |    3.1054 |    0.0019 |   0.0057 |         2 | False              |
| XAGUSD   |        72 | har_ewma | har     | 535 |          -0.0032 |   -0.8742 |    0.3820 |   0.7640 |         2 | False              |
| XAGUSD   |        72 | naive    | har     | 535 |           0.0013 |    0.2748 |    0.7835 |   0.7835 |         2 | False              |
| XAUUSD   |         6 | ewma     | har     | 541 |           0.0013 |    0.1978 |    0.8432 |   0.8432 |         0 | False              |
| XAUUSD   |         6 | har_ewma | har     | 541 |          -0.0080 |   -1.7979 |    0.0722 |   0.1444 |         0 | False              |
| XAUUSD   |         6 | naive    | har     | 541 |           0.1064 |    7.7211 |    0.0000 |   0.0000 |         0 | False              |
| XAUUSD   |        24 | ewma     | har     | 539 |           0.0127 |    3.0648 |    0.0022 |   0.0044 |         0 | False              |
| XAUUSD   |        24 | har_ewma | har     | 539 |          -0.0001 |   -0.0545 |    0.9566 |   0.9566 |         0 | False              |
| XAUUSD   |        24 | naive    | har     | 539 |           0.0223 |    4.5308 |    0.0000 |   0.0000 |         0 | False              |
| XAUUSD   |        48 | ewma     | har     | 537 |           0.0153 |    3.5557 |    0.0004 |   0.0011 |         1 | False              |
| XAUUSD   |        48 | har_ewma | har     | 537 |          -0.0003 |   -0.1219 |    0.9030 |   0.9030 |         1 | False              |
| XAUUSD   |        48 | naive    | har     | 537 |           0.0104 |    2.4539 |    0.0141 |   0.0283 |         1 | False              |
| XAUUSD   |        72 | ewma     | har     | 535 |           0.0165 |    3.4635 |    0.0005 |   0.0016 |         2 | False              |
| XAUUSD   |        72 | har_ewma | har     | 535 |          -0.0007 |   -0.2529 |    0.8003 |   1.0000 |         2 | False              |
| XAUUSD   |        72 | naive    | har     | 535 |           0.0023 |    0.5511 |    0.5816 |   1.0000 |         2 | False              |

#### vs `naive`

| symbol   |   horizon | arm_a    | arm_b   |   n |   mean_loss_diff |   dm_stat |   p_value |   p_holm |   hac_lag | significant_holm   |
|:---------|----------:|:---------|:--------|----:|-----------------:|----------:|----------:|---------:|----------:|:-------------------|
| BTCUSD   |         6 | ewma     | naive   | 698 |          -0.0659 |   -4.4438 |    0.0000 |   0.0000 |         0 | True               |
| BTCUSD   |         6 | har      | naive   | 697 |          -0.0604 |   -4.3449 |    0.0000 |   0.0000 |         0 | True               |
| BTCUSD   |         6 | har_ewma | naive   | 697 |          -0.0719 |   -5.1283 |    0.0000 |   0.0000 |         0 | True               |
| BTCUSD   |        24 | ewma     | naive   | 697 |          -0.0414 |   -7.3227 |    0.0000 |   0.0000 |         0 | True               |
| BTCUSD   |        24 | har      | naive   | 695 |          -0.0547 |   -6.5292 |    0.0000 |   0.0000 |         0 | True               |
| BTCUSD   |        24 | har_ewma | naive   | 695 |          -0.0575 |   -8.2676 |    0.0000 |   0.0000 |         0 | True               |
| BTCUSD   |        48 | ewma     | naive   | 696 |          -0.0007 |   -0.2037 |    0.8386 |   0.8386 |         1 | False              |
| BTCUSD   |        48 | har      | naive   | 693 |          -0.0312 |   -5.4588 |    0.0000 |   0.0000 |         1 | True               |
| BTCUSD   |        48 | har_ewma | naive   | 693 |          -0.0303 |   -7.1531 |    0.0000 |   0.0000 |         1 | True               |
| BTCUSD   |        72 | ewma     | naive   | 695 |           0.0158 |    4.2012 |    0.0000 |   0.0001 |         2 | False              |
| BTCUSD   |        72 | har      | naive   | 691 |          -0.0233 |   -4.1873 |    0.0000 |   0.0001 |         2 | True               |
| BTCUSD   |        72 | har_ewma | naive   | 691 |          -0.0205 |   -5.0394 |    0.0000 |   0.0000 |         2 | True               |
| USDCHF   |         6 | ewma     | naive   | 686 |          -0.1160 |   -6.8832 |    0.0000 |   0.0000 |         0 | True               |
| USDCHF   |         6 | har      | naive   | 685 |          -0.1390 |   -8.3347 |    0.0000 |   0.0000 |         0 | True               |
| USDCHF   |         6 | har_ewma | naive   | 685 |          -0.1345 |   -8.1171 |    0.0000 |   0.0000 |         0 | True               |
| USDCHF   |        24 | ewma     | naive   | 685 |          -0.0151 |   -5.3708 |    0.0000 |   0.0000 |         0 | True               |
| USDCHF   |        24 | har      | naive   | 683 |          -0.0316 |   -5.5305 |    0.0000 |   0.0000 |         0 | True               |
| USDCHF   |        24 | har_ewma | naive   | 683 |          -0.0319 |   -7.9374 |    0.0000 |   0.0000 |         0 | True               |
| USDCHF   |        48 | ewma     | naive   | 684 |           0.0020 |    0.8489 |    0.3959 |   0.3959 |         1 | False              |
| USDCHF   |        48 | har      | naive   | 681 |          -0.0160 |   -2.5715 |    0.0101 |   0.0202 |         1 | True               |
| USDCHF   |        48 | har_ewma | naive   | 681 |          -0.0192 |   -6.4041 |    0.0000 |   0.0000 |         1 | True               |
| USDCHF   |        72 | ewma     | naive   | 683 |           0.0090 |    3.3481 |    0.0008 |   0.0016 |         2 | False              |
| USDCHF   |        72 | har      | naive   | 679 |          -0.0094 |   -1.0664 |    0.2862 |   0.2862 |         2 | False              |
| USDCHF   |        72 | har_ewma | naive   | 679 |          -0.0158 |   -5.6307 |    0.0000 |   0.0000 |         2 | True               |
| USDJPY   |         6 | ewma     | naive   | 685 |          -0.1167 |   -7.2404 |    0.0000 |   0.0000 |         0 | True               |
| USDJPY   |         6 | har      | naive   | 684 |          -0.1149 |   -6.9689 |    0.0000 |   0.0000 |         0 | True               |
| USDJPY   |         6 | har_ewma | naive   | 684 |          -0.1257 |   -7.8507 |    0.0000 |   0.0000 |         0 | True               |
| USDJPY   |        24 | ewma     | naive   | 684 |          -0.0204 |   -3.8218 |    0.0001 |   0.0001 |         0 | True               |
| USDJPY   |        24 | har      | naive   | 682 |          -0.0297 |   -4.7750 |    0.0000 |   0.0000 |         0 | True               |
| USDJPY   |        24 | har_ewma | naive   | 682 |          -0.0336 |   -6.3510 |    0.0000 |   0.0000 |         0 | True               |
| USDJPY   |        48 | ewma     | naive   | 683 |          -0.0039 |   -0.9879 |    0.3232 |   0.3232 |         1 | False              |
| USDJPY   |        48 | har      | naive   | 680 |          -0.0230 |   -3.2409 |    0.0012 |   0.0024 |         1 | True               |
| USDJPY   |        48 | har_ewma | naive   | 680 |          -0.0246 |   -4.9012 |    0.0000 |   0.0000 |         1 | True               |
| USDJPY   |        72 | ewma     | naive   | 682 |           0.0053 |    0.9492 |    0.3425 |   0.3425 |         2 | False              |
| USDJPY   |        72 | har      | naive   | 678 |          -0.0263 |   -2.5731 |    0.0101 |   0.0202 |         2 | True               |
| USDJPY   |        72 | har_ewma | naive   | 678 |          -0.0249 |   -3.2631 |    0.0011 |   0.0033 |         2 | True               |
| XAGUSD   |         6 | ewma     | naive   | 542 |          -0.1417 |   -5.7401 |    0.0000 |   0.0000 |         0 | True               |
| XAGUSD   |         6 | har      | naive   | 541 |          -0.1466 |   -5.8210 |    0.0000 |   0.0000 |         0 | True               |
| XAGUSD   |         6 | har_ewma | naive   | 541 |          -0.1536 |   -6.2170 |    0.0000 |   0.0000 |         0 | True               |
| XAGUSD   |        24 | ewma     | naive   | 541 |          -0.0158 |   -5.6176 |    0.0000 |   0.0000 |         0 | True               |
| XAGUSD   |        24 | har      | naive   | 539 |          -0.0318 |   -4.7697 |    0.0000 |   0.0000 |         0 | True               |
| XAGUSD   |        24 | har_ewma | naive   | 539 |          -0.0318 |   -7.9616 |    0.0000 |   0.0000 |         0 | True               |
| XAGUSD   |        48 | ewma     | naive   | 540 |           0.0065 |    2.9024 |    0.0037 |   0.0074 |         1 | False              |
| XAGUSD   |        48 | har      | naive   | 537 |          -0.0099 |   -1.8722 |    0.0612 |   0.0612 |         1 | False              |
| XAGUSD   |        48 | har_ewma | naive   | 537 |          -0.0124 |   -4.7416 |    0.0000 |   0.0000 |         1 | True               |
| XAGUSD   |        72 | ewma     | naive   | 539 |           0.0154 |    5.7277 |    0.0000 |   0.0000 |         2 | False              |
| XAGUSD   |        72 | har      | naive   | 535 |          -0.0013 |   -0.2748 |    0.7835 |   0.7835 |         2 | False              |
| XAGUSD   |        72 | har_ewma | naive   | 535 |          -0.0045 |   -1.9807 |    0.0476 |   0.0952 |         2 | False              |
| XAUUSD   |         6 | ewma     | naive   | 542 |          -0.1051 |   -7.6583 |    0.0000 |   0.0000 |         0 | True               |
| XAUUSD   |         6 | har      | naive   | 541 |          -0.1064 |   -7.7211 |    0.0000 |   0.0000 |         0 | True               |
| XAUUSD   |         6 | har_ewma | naive   | 541 |          -0.1145 |   -8.5863 |    0.0000 |   0.0000 |         0 | True               |
| XAUUSD   |        24 | ewma     | naive   | 541 |          -0.0096 |   -4.0680 |    0.0000 |   0.0000 |         0 | True               |
| XAUUSD   |        24 | har      | naive   | 539 |          -0.0223 |   -4.5308 |    0.0000 |   0.0000 |         0 | True               |
| XAUUSD   |        24 | har_ewma | naive   | 539 |          -0.0225 |   -7.1457 |    0.0000 |   0.0000 |         0 | True               |
| XAUUSD   |        48 | ewma     | naive   | 540 |           0.0049 |    2.3787 |    0.0174 |   0.0283 |         1 | False              |
| XAUUSD   |        48 | har      | naive   | 537 |          -0.0104 |   -2.4539 |    0.0141 |   0.0283 |         1 | True               |
| XAUUSD   |        48 | har_ewma | naive   | 537 |          -0.0108 |   -4.4773 |    0.0000 |   0.0000 |         1 | True               |
| XAUUSD   |        72 | ewma     | naive   | 539 |           0.0142 |    4.8305 |    0.0000 |   0.0000 |         2 | False              |
| XAUUSD   |        72 | har      | naive   | 535 |          -0.0023 |   -0.5511 |    0.5816 |   0.5816 |         2 | False              |
| XAUUSD   |        72 | har_ewma | naive   | 535 |          -0.0030 |   -1.3126 |    0.1893 |   0.3787 |         2 | False              |

## Arm runtime

| arm | calls | successes | seconds |
|---|---|---|---|
| `ewma` | 12582 | 12582 | 2.9 |
| `har` | 12582 | 12532 | 10.7 |
| `har_ewma` | 12582 | 12532 | 14.1 |
| `naive` | 12582 | 12582 | 0.4 |

## Honesty notes

- Model arms were executed only if listed above under successes; `chronos2*`/`timesfm25` require `chronos-forecasting>=2.0` or the :8002 server respectively. Synthetic runs validate METHODOLOGY, not financial skill.
- Coverage is the empirical 80% central-interval coverage; acceptance band [0.80, 0.95] (too narrow or too wide both fail).
- MAE on the raw scale is per-symbol only — do not average across symbols; cross-symbol comparison lives in qlike/mae_log.
- Nothing here is wired into the live orchestrator/EV/Kelly path. Phase 3/4 integration is gated on these results.
