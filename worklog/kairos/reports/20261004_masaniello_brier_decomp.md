# Masaniello Brier Decomposition — 20261004

Lookback: 45d (since 2026-08-20T03:50Z)

Min symbols N: 200
Batch size N: 5, target wins W: 3

Symbols: 14


## AAPL — n=6097 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6097
  batches: 1220
  M_i mean=0.9009  min=0.0000  max=1.5000
  drift mean=1.5016  min=0.0000  max=2.5000
  high-urgency trades: 1871 (30.7%)

### Brier decomposition


  **p_win → directional**
  n=4884  base_rate=0.5029  Brier=0.3224  REL=0.0908  RES=0.0205  UNC=0.2500  BSS=-0.2898  AUC=0.3892
      0.0-0.1  n=   17  pred=0.0842  realised=0.9412
      0.1-0.2  n=  794  pred=0.1802  realised=0.5504
      0.2-0.3  n=  936  pred=0.2290  realised=0.7094
      0.3-0.4  n=  381  pred=0.3560  realised=0.5853
      0.4-0.5  n=  797  pred=0.4537  realised=0.4755
      0.5-0.6  n= 1495  pred=0.5421  realised=0.3318
      0.6-0.7  n=  250  pred=0.6357  realised=0.4200
      0.7-0.8  n=  174  pred=0.7499  realised=0.6897
      0.8-0.9  n=   40  pred=0.8244  realised=0.4000

  **p_win → bracket**
  n=5963  base_rate=0.2088  Brier=0.2492  REL=0.0907  RES=0.0060  UNC=0.1652  BSS=-0.5084  AUC=0.4513
      0.0-0.1  n=   19  pred=0.0840  realised=0.1579
      0.1-0.2  n=  861  pred=0.1773  realised=0.1940
      0.2-0.3  n= 1042  pred=0.2314  realised=0.2102
      0.3-0.4  n=  593  pred=0.3565  realised=0.3288
      0.4-0.5  n= 1011  pred=0.4554  realised=0.3234
      0.5-0.6  n= 1756  pred=0.5437  realised=0.1321
      0.6-0.7  n=  316  pred=0.6349  realised=0.1772
      0.7-0.8  n=  249  pred=0.7538  realised=0.1566
      0.8-0.9  n=  116  pred=0.8345  realised=0.0603

  **predicted_p_win_raw → directional**
  n=4884  base_rate=0.5029  Brier=0.3195  REL=0.0978  RES=0.0292  UNC=0.2500  BSS=-0.2782  AUC=0.4327
      0.0-0.1  n=   27  pred=0.0813  realised=0.7407
      0.1-0.2  n=  936  pred=0.1774  realised=0.4925
      0.2-0.3  n= 1013  pred=0.2315  realised=0.7137
      0.3-0.4  n=  425  pred=0.3595  realised=0.5224
      0.4-0.5  n=  746  pred=0.4539  realised=0.4946
      0.5-0.6  n= 1006  pred=0.5530  realised=0.2445
      0.6-0.7  n=  357  pred=0.6378  realised=0.3978
      0.7-0.8  n=  324  pred=0.7502  realised=0.7778
      0.8-0.9  n=   50  pred=0.8240  realised=0.4200

  **predicted_p_win_raw → bracket**
  n=5963  base_rate=0.2088  Brier=0.2514  REL=0.0935  RES=0.0072  UNC=0.1652  BSS=-0.5219  AUC=0.4770
      0.0-0.1  n=   30  pred=0.0817  realised=0.2000
      0.1-0.2  n= 1032  pred=0.1741  realised=0.2006
      0.2-0.3  n= 1118  pred=0.2331  realised=0.2129
      0.3-0.4  n=  614  pred=0.3573  realised=0.2883
      0.4-0.5  n=  954  pred=0.4553  realised=0.3124
      0.5-0.6  n= 1181  pred=0.5528  realised=0.0686
      0.6-0.7  n=  416  pred=0.6388  realised=0.2019
      0.7-0.8  n=  484  pred=0.7532  realised=0.2955
      0.8-0.9  n=  134  pred=0.8334  realised=0.0821

  **p_win_bracket → bracket**
  n=1280  base_rate=0.3180  Brier=0.2433  REL=0.0690  RES=0.0430  UNC=0.2169  BSS=-0.1218  AUC=0.3994
      0.1-0.2  n=  873  pred=0.1545  realised=0.3940
      0.2-0.3  n=  140  pred=0.2293  realised=0.0000
      0.3-0.4  n=  118  pred=0.3195  realised=0.0000
      0.4-0.5  n=    4  pred=0.4045  realised=0.0000
      0.5-0.6  n=   65  pred=0.5600  realised=0.0615
      0.6-0.7  n=   34  pred=0.6361  realised=0.4412
      0.8-0.9  n=   46  pred=0.8333  realised=0.9565

  **p_win_dir → directional**
  n=849  base_rate=0.5524  Brier=0.2467  REL=0.0495  RES=0.0501  UNC=0.2473  BSS=+0.0024  AUC=0.6315
      0.0-0.1  n=    2  pred=0.0915  realised=1.0000
      0.1-0.2  n=   13  pred=0.1589  realised=0.7692
      0.2-0.3  n=   62  pred=0.2504  realised=0.2903
      0.3-0.4  n=   61  pred=0.3595  realised=0.5574
      0.4-0.5  n=   78  pred=0.4480  realised=0.6795
      0.5-0.6  n=  182  pred=0.5583  realised=0.2088
      0.6-0.7  n=  177  pred=0.6523  realised=0.6441
      0.7-0.8  n=  239  pred=0.7462  realised=0.7782
      0.8-0.9  n=   35  pred=0.8259  realised=0.4000

### Calibration stratified by urgency


  high-urgency n=1871, on-track n=4226
  **p_win → directional** [high-urgency batch]
  n=1542  base_rate=0.4559  Brier=0.3371  REL=0.1284  RES=0.0420  UNC=0.2481  BSS=-0.3589  AUC=0.3229

  high-urgency n=1871, on-track n=4226
  **p_win → directional** [on-track batch]
  n=3342  base_rate=0.5245  Brier=0.3157  REL=0.0788  RES=0.0146  UNC=0.2494  BSS=-0.2658  AUC=0.4206

---

## AUDUSD — n=6110 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6110
  batches: 1222
  M_i mean=0.6440  min=0.0000  max=1.5000
  drift mean=1.0734  min=0.0000  max=2.5000
  high-urgency trades: 264 (4.3%)

### Brier decomposition


  **p_win → directional**
  n=5493  base_rate=0.5625  Brier=0.2701  REL=0.0279  RES=0.0035  UNC=0.2461  BSS=-0.0976  AUC=0.5659
      0.0-0.1  n=   32  pred=0.0868  realised=0.3125
      0.1-0.2  n=  408  pred=0.1622  realised=0.4510
      0.2-0.3  n=  706  pred=0.2545  realised=0.5241
      0.3-0.4  n=  855  pred=0.3467  realised=0.5287
      0.4-0.5  n=  901  pred=0.4528  realised=0.5505
      0.5-0.6  n=  861  pred=0.5346  realised=0.5679
      0.6-0.7  n=  456  pred=0.6471  realised=0.5833
      0.7-0.8  n=  857  pred=0.7572  realised=0.6464
      0.8-0.9  n=  384  pred=0.8318  realised=0.6302
      0.9-1.0  n=   33  pred=0.9137  realised=0.8182

  **p_win → bracket**
  n=1015  base_rate=0.2729  Brier=0.2998  REL=0.1223  RES=0.0207  UNC=0.1984  BSS=-0.5110  AUC=0.4123
      0.0-0.1  n=    8  pred=0.0880  realised=0.3750
      0.1-0.2  n=   53  pred=0.1707  realised=0.0377
      0.2-0.3  n=  167  pred=0.2571  realised=0.3713
      0.3-0.4  n=  163  pred=0.3471  realised=0.2822
      0.4-0.5  n=  244  pred=0.4487  realised=0.4303
      0.5-0.6  n=  144  pred=0.5393  realised=0.2778
      0.6-0.7  n=   76  pred=0.6533  realised=0.1053
      0.7-0.8  n=   78  pred=0.7473  realised=0.1282
      0.8-0.9  n=   81  pred=0.8386  realised=0.0000
      0.9-1.0  n=    1  pred=0.9031  realised=1.0000

  **predicted_p_win_raw → directional**
  n=5493  base_rate=0.5625  Brier=0.2748  REL=0.0324  RES=0.0043  UNC=0.2461  BSS=-0.1166  AUC=0.5681
      0.0-0.1  n=   51  pred=0.0881  realised=0.2941
      0.1-0.2  n=  509  pred=0.1566  realised=0.4833
      0.2-0.3  n=  793  pred=0.2538  realised=0.5359
      0.3-0.4  n=  922  pred=0.3430  realised=0.4913
      0.4-0.5  n=  867  pred=0.4526  realised=0.5536
      0.5-0.6  n=  583  pred=0.5494  realised=0.6038
      0.6-0.7  n=  469  pred=0.6474  realised=0.5991
      0.7-0.8  n=  871  pred=0.7571  realised=0.6418
      0.8-0.9  n=  394  pred=0.8320  realised=0.6396
      0.9-1.0  n=   34  pred=0.9134  realised=0.7941

  **predicted_p_win_raw → bracket**
  n=1015  base_rate=0.2729  Brier=0.3190  REL=0.1354  RES=0.0162  UNC=0.1984  BSS=-0.6076  AUC=0.3982
      0.0-0.1  n=    9  pred=0.0865  realised=0.4444
      0.1-0.2  n=   73  pred=0.1617  realised=0.1233
      0.2-0.3  n=  198  pred=0.2522  realised=0.3889
      0.3-0.4  n=  161  pred=0.3450  realised=0.3043
      0.4-0.5  n=  202  pred=0.4510  realised=0.3960
      0.5-0.6  n=   76  pred=0.5500  realised=0.3158
      0.6-0.7  n=   83  pred=0.6539  realised=0.1687
      0.7-0.8  n=   99  pred=0.7510  realised=0.1414
      0.8-0.9  n=  112  pred=0.8393  realised=0.0446
      0.9-1.0  n=    2  pred=0.9031  realised=0.5000

  **p_win_bracket → bracket**
  n=421  base_rate=0.1401  Brier=0.3417  REL=0.2330  RES=0.0131  UNC=0.1205  BSS=-1.8352  AUC=0.4372
      0.0-0.1  n=   98  pred=0.0121  realised=0.1531
      0.1-0.2  n=   14  pred=0.1667  realised=0.5714
      0.2-0.3  n=    9  pred=0.2073  realised=0.0000
      0.3-0.4  n=   38  pred=0.3178  realised=0.3158
      0.5-0.6  n=   10  pred=0.5500  realised=0.1000
      0.6-0.7  n=  184  pred=0.6723  realised=0.0543
      0.7-0.8  n=   68  pred=0.7492  realised=0.1912

  **p_win_dir → directional**
  n=1273  base_rate=0.4792  Brier=0.2368  REL=0.0073  RES=0.0190  UNC=0.2496  BSS=+0.0511  AUC=0.6450
      0.0-0.1  n=   14  pred=0.0838  realised=0.0714
      0.1-0.2  n=  104  pred=0.1594  realised=0.2115
      0.2-0.3  n=  146  pred=0.2528  realised=0.3288
      0.3-0.4  n=  215  pred=0.3479  realised=0.3907
      0.4-0.5  n=  257  pred=0.4548  realised=0.5292
      0.5-0.6  n=  275  pred=0.5492  realised=0.6073
      0.6-0.7  n=  154  pred=0.6382  realised=0.5844
      0.7-0.8  n=   81  pred=0.7439  realised=0.5185
      0.8-0.9  n=   26  pred=0.8339  realised=0.7692
      0.9-1.0  n=    1  pred=0.9030  realised=0.0000

### Calibration stratified by urgency


  high-urgency n=264, on-track n=5846
  **p_win → directional** [high-urgency batch]
  n=166  base_rate=0.2892  Brier=0.2319  REL=0.0391  RES=0.0142  UNC=0.2055  BSS=-0.1284  AUC=0.6109

  high-urgency n=264, on-track n=5846
  **p_win → directional** [on-track batch]
  n=5327  base_rate=0.5711  Brier=0.2713  REL=0.0301  RES=0.0033  UNC=0.2450  BSS=-0.1075  AUC=0.5628

---

## BTCUSD — n=6074 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6074
  batches: 1215
  M_i mean=0.7443  min=0.0000  max=1.5000
  drift mean=1.2405  min=0.0000  max=2.5000
  high-urgency trades: 1099 (18.1%)

### Brier decomposition


  **p_win → directional**
  n=5388  base_rate=0.5457  Brier=0.2777  REL=0.0312  RES=0.0017  UNC=0.2479  BSS=-0.1202  AUC=0.4908
      0.0-0.1  n=   15  pred=0.0878  realised=0.6667
      0.1-0.2  n=  182  pred=0.1635  realised=0.5440
      0.2-0.3  n=  548  pred=0.2566  realised=0.5566
      0.3-0.4  n=  905  pred=0.3524  realised=0.5624
      0.4-0.5  n= 1584  pred=0.4538  realised=0.5518
      0.5-0.6  n= 1091  pred=0.5443  realised=0.5353
      0.6-0.7  n=  683  pred=0.6428  realised=0.4832
      0.7-0.8  n=  285  pred=0.7395  realised=0.5789
      0.8-0.9  n=   87  pred=0.8334  realised=0.7356
      0.9-1.0  n=    8  pred=0.9132  realised=0.0000

  **p_win → bracket**
  n=4843  base_rate=0.3768  Brier=0.2647  REL=0.0324  RES=0.0041  UNC=0.2348  BSS=-0.1272  AUC=0.4976
      0.0-0.1  n=   10  pred=0.0880  realised=0.5000
      0.1-0.2  n=  144  pred=0.1643  realised=0.4583
      0.2-0.3  n=  423  pred=0.2558  realised=0.4515
      0.3-0.4  n=  892  pred=0.3536  realised=0.3408
      0.4-0.5  n= 1453  pred=0.4557  realised=0.3682
      0.5-0.6  n=  969  pred=0.5453  realised=0.3354
      0.6-0.7  n=  623  pred=0.6399  realised=0.3499
      0.7-0.8  n=  229  pred=0.7432  realised=0.5415
      0.8-0.9  n=   92  pred=0.8334  realised=0.6196
      0.9-1.0  n=    8  pred=0.9132  realised=0.0000

  **predicted_p_win_raw → directional**
  n=5388  base_rate=0.5457  Brier=0.2816  REL=0.0346  RES=0.0016  UNC=0.2479  BSS=-0.1358  AUC=0.4967
      0.0-0.1  n=   20  pred=0.0863  realised=0.5000
      0.1-0.2  n=  240  pred=0.1619  realised=0.5042
      0.2-0.3  n=  673  pred=0.2559  realised=0.5453
      0.3-0.4  n=  916  pred=0.3513  realised=0.5349
      0.4-0.5  n= 1407  pred=0.4504  realised=0.5828
      0.5-0.6  n=  934  pred=0.5493  realised=0.5535
      0.6-0.7  n=  730  pred=0.6439  realised=0.4836
      0.7-0.8  n=  338  pred=0.7398  realised=0.5533
      0.8-0.9  n=  122  pred=0.8342  realised=0.6148
      0.9-1.0  n=    8  pred=0.9132  realised=0.0000

  **predicted_p_win_raw → bracket**
  n=4843  base_rate=0.3768  Brier=0.2688  REL=0.0349  RES=0.0029  UNC=0.2348  BSS=-0.1446  AUC=0.4963
      0.0-0.1  n=   14  pred=0.0856  realised=0.3571
      0.1-0.2  n=  192  pred=0.1628  realised=0.4375
      0.2-0.3  n=  534  pred=0.2554  realised=0.4326
      0.3-0.4  n=  891  pred=0.3511  realised=0.3244
      0.4-0.5  n= 1293  pred=0.4541  realised=0.3805
      0.5-0.6  n=  862  pred=0.5490  realised=0.3469
      0.6-0.7  n=  649  pred=0.6411  realised=0.3436
      0.7-0.8  n=  275  pred=0.7426  realised=0.5164
      0.8-0.9  n=  125  pred=0.8336  realised=0.4800
      0.9-1.0  n=    8  pred=0.9132  realised=0.0000

  **p_win_bracket → bracket**
  n=901  base_rate=0.3052  Brier=0.2205  REL=0.0183  RES=0.0109  UNC=0.2121  BSS=-0.0396  AUC=0.5376
      0.1-0.2  n=   40  pred=0.1432  realised=0.4750
      0.2-0.3  n=  281  pred=0.2595  realised=0.2740
      0.3-0.4  n=  321  pred=0.3281  realised=0.2430
      0.4-0.5  n=   67  pred=0.4564  realised=0.4478
      0.5-0.6  n=  119  pred=0.5218  realised=0.2437
      0.6-0.7  n=   73  pred=0.6609  realised=0.5753

  **p_win_dir → directional**
  n=1324  base_rate=0.4645  Brier=0.3094  REL=0.0673  RES=0.0067  UNC=0.2487  BSS=-0.2439  AUC=0.4519
      0.0-0.1  n=   17  pred=0.0851  realised=0.5294
      0.1-0.2  n=  160  pred=0.1563  realised=0.5250
      0.2-0.3  n=  216  pred=0.2511  realised=0.4491
      0.3-0.4  n=  215  pred=0.3499  realised=0.4744
      0.4-0.5  n=  206  pred=0.4463  realised=0.5485
      0.5-0.6  n=  213  pred=0.5477  realised=0.5164
      0.6-0.7  n=  120  pred=0.6473  realised=0.3833
      0.7-0.8  n=  109  pred=0.7398  realised=0.3028
      0.8-0.9  n=   60  pred=0.8338  realised=0.3500
      0.9-1.0  n=    8  pred=0.9132  realised=0.0000

### Calibration stratified by urgency


  high-urgency n=1099, on-track n=4975
  **p_win → directional** [high-urgency batch]
  n=895  base_rate=0.3028  Brier=0.2823  REL=0.0780  RES=0.0084  UNC=0.2111  BSS=-0.3371  AUC=0.3947

  high-urgency n=1099, on-track n=4975
  **p_win → directional** [on-track batch]
  n=4493  base_rate=0.5940  Brier=0.2768  REL=0.0374  RES=0.0018  UNC=0.2412  BSS=-0.1478  AUC=0.5080

---

## EURUSD — n=6112 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6112
  batches: 1223
  M_i mean=0.6377  min=0.0000  max=1.5000
  drift mean=1.0629  min=0.0000  max=2.5000
  high-urgency trades: 236 (3.9%)

### Brier decomposition


  **p_win → directional**
  n=5468  base_rate=0.5488  Brier=0.2823  REL=0.0363  RES=0.0023  UNC=0.2476  BSS=-0.1402  AUC=0.5075
      0.0-0.1  n=   34  pred=0.0814  realised=0.2353
      0.1-0.2  n=  165  pred=0.1617  realised=0.4788
      0.2-0.3  n=  421  pred=0.2520  realised=0.5416
      0.3-0.4  n=  529  pred=0.3506  realised=0.5747
      0.4-0.5  n= 1135  pred=0.4504  realised=0.5789
      0.5-0.6  n=  660  pred=0.5488  realised=0.4742
      0.6-0.7  n=  747  pred=0.6517  realised=0.5248
      0.7-0.8  n= 1222  pred=0.7558  realised=0.5966
      0.8-0.9  n=  547  pred=0.8338  realised=0.5247
      0.9-1.0  n=    8  pred=0.9170  realised=0.5000

  **p_win → bracket**
  n=990  base_rate=0.2889  Brier=0.2683  REL=0.0673  RES=0.0052  UNC=0.2054  BSS=-0.3060  AUC=0.5749
      0.0-0.1  n=   11  pred=0.0829  realised=0.2727
      0.1-0.2  n=   38  pred=0.1630  realised=0.2105
      0.2-0.3  n=   96  pred=0.2523  realised=0.2188
      0.3-0.4  n=  220  pred=0.3563  realised=0.1909
      0.4-0.5  n=  204  pred=0.4543  realised=0.3333
      0.5-0.6  n=   96  pred=0.5443  realised=0.3125
      0.6-0.7  n=  113  pred=0.6470  realised=0.3805
      0.7-0.8  n=  119  pred=0.7525  realised=0.3025
      0.8-0.9  n=   85  pred=0.8255  realised=0.3882
      0.9-1.0  n=    8  pred=0.9112  realised=0.2500

  **predicted_p_win_raw → directional**
  n=5468  base_rate=0.5488  Brier=0.2836  REL=0.0383  RES=0.0029  UNC=0.2476  BSS=-0.1454  AUC=0.5101
      0.0-0.1  n=   39  pred=0.0821  realised=0.2051
      0.1-0.2  n=  225  pred=0.1583  realised=0.4000
      0.2-0.3  n=  421  pred=0.2503  realised=0.5724
      0.3-0.4  n=  513  pred=0.3514  realised=0.6082
      0.4-0.5  n= 1117  pred=0.4488  realised=0.5604
      0.5-0.6  n=  576  pred=0.5519  realised=0.5191
      0.6-0.7  n=  748  pred=0.6517  realised=0.5107
      0.7-0.8  n= 1252  pred=0.7561  realised=0.5887
      0.8-0.9  n=  566  pred=0.8333  realised=0.5283
      0.9-1.0  n=   11  pred=0.9175  realised=0.6364

  **predicted_p_win_raw → bracket**
  n=990  base_rate=0.2889  Brier=0.2728  REL=0.0707  RES=0.0044  UNC=0.2054  BSS=-0.3281  AUC=0.5619
      0.0-0.1  n=   11  pred=0.0829  realised=0.2727
      0.1-0.2  n=   57  pred=0.1603  realised=0.1930
      0.2-0.3  n=  112  pred=0.2521  realised=0.2589
      0.3-0.4  n=  211  pred=0.3557  realised=0.2085
      0.4-0.5  n=  178  pred=0.4560  realised=0.3090
      0.5-0.6  n=   88  pred=0.5433  realised=0.3068
      0.6-0.7  n=  111  pred=0.6488  realised=0.4144
      0.7-0.8  n=  118  pred=0.7516  realised=0.3051
      0.8-0.9  n=   94  pred=0.8283  realised=0.3511
      0.9-1.0  n=   10  pred=0.9167  realised=0.2000

  **p_win_bracket → bracket**
  n=490  base_rate=0.2204  Brier=0.2545  REL=0.0938  RES=0.0110  UNC=0.1718  BSS=-0.4809  AUC=0.4834
      0.0-0.1  n=  145  pred=0.0096  realised=0.1655
      0.1-0.2  n=   17  pred=0.1364  realised=0.2353
      0.3-0.4  n=  114  pred=0.3666  realised=0.3860
      0.4-0.5  n=   68  pred=0.4434  realised=0.0882
      0.5-0.6  n=   10  pred=0.5349  realised=0.4000
      0.6-0.7  n=  115  pred=0.6750  realised=0.1739
      0.7-0.8  n=   21  pred=0.7537  realised=0.2857

  **p_win_dir → directional**
  n=1273  base_rate=0.4745  Brier=0.2663  REL=0.0348  RES=0.0171  UNC=0.2493  BSS=-0.0681  AUC=0.5864
      0.0-0.1  n=   20  pred=0.0832  realised=0.0000
      0.1-0.2  n=  111  pred=0.1569  realised=0.2252
      0.2-0.3  n=  135  pred=0.2497  realised=0.5259
      0.3-0.4  n=  140  pred=0.3550  realised=0.5786
      0.4-0.5  n=  260  pred=0.4427  realised=0.4038
      0.5-0.6  n=  122  pred=0.5514  realised=0.5410
      0.6-0.7  n=  130  pred=0.6427  realised=0.3923
      0.7-0.8  n=  173  pred=0.7627  realised=0.5145
      0.8-0.9  n=  177  pred=0.8266  realised=0.6271
      0.9-1.0  n=    5  pred=0.9174  realised=1.0000

### Calibration stratified by urgency


  high-urgency n=236, on-track n=5876
  **p_win → directional** [high-urgency batch]
  n=152  base_rate=0.2303  Brier=0.3366  REL=0.1852  RES=0.0266  UNC=0.1772  BSS=-0.8991  AUC=0.3662

  high-urgency n=236, on-track n=5876
  **p_win → directional** [on-track batch]
  n=5316  base_rate=0.5579  Brier=0.2808  REL=0.0355  RES=0.0023  UNC=0.2466  BSS=-0.1384  AUC=0.5082

---

## GBPUSD — n=6113 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6113
  batches: 1223
  M_i mean=0.6606  min=0.0000  max=1.5000
  drift mean=1.1011  min=0.0000  max=2.5000
  high-urgency trades: 352 (5.8%)

### Brier decomposition


  **p_win → directional**
  n=5402  base_rate=0.5629  Brier=0.2905  REL=0.0512  RES=0.0080  UNC=0.2460  BSS=-0.1806  AUC=0.4832
      0.0-0.1  n=   30  pred=0.0853  realised=0.0667
      0.1-0.2  n=  263  pred=0.1579  realised=0.4867
      0.2-0.3  n=  395  pred=0.2488  realised=0.5544
      0.3-0.4  n=  429  pred=0.3503  realised=0.5874
      0.4-0.5  n= 1314  pred=0.4651  realised=0.5890
      0.5-0.6  n=  638  pred=0.5472  realised=0.5862
      0.6-0.7  n=  560  pred=0.6508  realised=0.6054
      0.7-0.8  n= 1090  pred=0.7541  realised=0.6349
      0.8-0.9  n=  611  pred=0.8386  realised=0.3568
      0.9-1.0  n=   72  pred=0.9216  realised=0.5972

  **p_win → bracket**
  n=1258  base_rate=0.2250  Brier=0.3232  REL=0.1591  RES=0.0115  UNC=0.1744  BSS=-0.8536  AUC=0.3960
      0.0-0.1  n=   19  pred=0.0866  realised=0.0000
      0.1-0.2  n=   74  pred=0.1568  realised=0.2297
      0.2-0.3  n=  129  pred=0.2569  realised=0.2403
      0.3-0.4  n=  148  pred=0.3502  realised=0.2973
      0.4-0.5  n=  264  pred=0.4589  realised=0.3561
      0.5-0.6  n=  209  pred=0.5411  realised=0.2057
      0.6-0.7  n=  130  pred=0.6578  realised=0.2538
      0.7-0.8  n=  146  pred=0.7478  realised=0.1438
      0.8-0.9  n=  117  pred=0.8274  realised=0.0000
      0.9-1.0  n=   22  pred=0.9104  realised=0.0000

  **predicted_p_win_raw → directional**
  n=5402  base_rate=0.5629  Brier=0.2906  REL=0.0520  RES=0.0085  UNC=0.2460  BSS=-0.1812  AUC=0.4873
      0.0-0.1  n=   47  pred=0.0841  realised=0.0426
      0.1-0.2  n=  317  pred=0.1543  realised=0.4448
      0.2-0.3  n=  360  pred=0.2493  realised=0.5833
      0.3-0.4  n=  429  pred=0.3497  realised=0.5991
      0.4-0.5  n= 1278  pred=0.4648  realised=0.5986
      0.5-0.6  n=  598  pred=0.5492  realised=0.5819
      0.6-0.7  n=  568  pred=0.6507  realised=0.6180
      0.7-0.8  n= 1038  pred=0.7543  realised=0.6281
      0.8-0.9  n=  693  pred=0.8384  realised=0.3925
      0.9-1.0  n=   74  pred=0.9213  realised=0.5811

  **predicted_p_win_raw → bracket**
  n=1258  base_rate=0.2250  Brier=0.3465  REL=0.1836  RES=0.0121  UNC=0.1744  BSS=-0.9871  AUC=0.3878
      0.0-0.1  n=   23  pred=0.0867  realised=0.0000
      0.1-0.2  n=  108  pred=0.1507  realised=0.2778
      0.2-0.3  n=  119  pred=0.2568  realised=0.2689
      0.3-0.4  n=  151  pred=0.3507  realised=0.3046
      0.4-0.5  n=  233  pred=0.4584  realised=0.3348
      0.5-0.6  n=  193  pred=0.5444  realised=0.2021
      0.6-0.7  n=   97  pred=0.6515  realised=0.2887
      0.7-0.8  n=  109  pred=0.7467  realised=0.2294
      0.8-0.9  n=  140  pred=0.8409  realised=0.0357
      0.9-1.0  n=   85  pred=0.9067  realised=0.0000

  **p_win_bracket → bracket**
  n=733  base_rate=0.2060  Brier=0.1966  REL=0.0419  RES=0.0081  UNC=0.1636  BSS=-0.2019  AUC=0.5182
      0.0-0.1  n=  318  pred=0.0103  realised=0.1824
      0.1-0.2  n=   26  pred=0.1653  realised=0.5385
      0.2-0.3  n=  177  pred=0.2373  realised=0.2147
      0.3-0.4  n=  108  pred=0.3367  realised=0.1574
      0.4-0.5  n=   29  pred=0.4054  realised=0.3448
      0.5-0.6  n=   42  pred=0.5278  realised=0.3333
      0.6-0.7  n=   33  pred=0.6129  realised=0.0000

  **p_win_dir → directional**
  n=1188  base_rate=0.5202  Brier=0.2319  REL=0.0195  RES=0.0377  UNC=0.2496  BSS=+0.0707  AUC=0.6795
      0.0-0.1  n=   37  pred=0.0842  realised=0.0000
      0.1-0.2  n=  150  pred=0.1525  realised=0.2267
      0.2-0.3  n=  116  pred=0.2509  realised=0.3534
      0.3-0.4  n=  154  pred=0.3538  realised=0.5065
      0.4-0.5  n=  157  pred=0.4537  realised=0.5350
      0.5-0.6  n=  197  pred=0.5513  realised=0.6294
      0.6-0.7  n=  175  pred=0.6480  realised=0.8000
      0.7-0.8  n=   96  pred=0.7483  realised=0.5208
      0.8-0.9  n=  103  pred=0.8365  realised=0.6505
      0.9-1.0  n=    3  pred=0.9103  realised=0.0000

### Calibration stratified by urgency


  high-urgency n=352, on-track n=5761
  **p_win → directional** [high-urgency batch]
  n=221  base_rate=0.3710  Brier=0.2405  REL=0.0276  RES=0.0204  UNC=0.2334  BSS=-0.0306  AUC=0.6082

  high-urgency n=352, on-track n=5761
  **p_win → directional** [on-track batch]
  n=5181  base_rate=0.5711  Brier=0.2926  REL=0.0545  RES=0.0081  UNC=0.2449  BSS=-0.1946  AUC=0.4727

---

## US30USD — n=6102 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6102
  batches: 1221
  M_i mean=0.6917  min=0.0000  max=1.5000
  drift mean=1.1529  min=0.0000  max=2.5000
  high-urgency trades: 619 (10.1%)

### Brier decomposition


  **p_win → directional**
  n=5377  base_rate=0.5457  Brier=0.2853  REL=0.0371  RES=0.0015  UNC=0.2479  BSS=-0.1508  AUC=0.4940
      0.0-0.1  n=   24  pred=0.0843  realised=0.6667
      0.1-0.2  n=  375  pred=0.1585  realised=0.6240
      0.2-0.3  n=  455  pred=0.2532  realised=0.5758
      0.3-0.4  n=  892  pred=0.3564  realised=0.4843
      0.4-0.5  n= 1671  pred=0.4540  realised=0.5440
      0.5-0.6  n=  831  pred=0.5407  realised=0.5548
      0.6-0.7  n=  524  pred=0.6476  realised=0.5095
      0.7-0.8  n=  404  pred=0.7474  realised=0.5916
      0.8-0.9  n=  197  pred=0.8372  realised=0.5635
      0.9-1.0  n=    4  pred=0.9117  realised=0.7500

  **p_win → bracket**
  n=2510  base_rate=0.3171  Brier=0.2888  REL=0.0810  RES=0.0093  UNC=0.2166  BSS=-0.3334  AUC=0.4336
      0.0-0.1  n=    7  pred=0.0893  realised=0.2857
      0.1-0.2  n=  189  pred=0.1553  realised=0.5926
      0.2-0.3  n=  215  pred=0.2534  realised=0.4186
      0.3-0.4  n=  426  pred=0.3532  realised=0.3122
      0.4-0.5  n=  545  pred=0.4513  realised=0.2294
      0.5-0.6  n=  586  pred=0.5387  realised=0.3106
      0.6-0.7  n=  282  pred=0.6486  realised=0.2660
      0.7-0.8  n=  185  pred=0.7407  realised=0.3243
      0.8-0.9  n=   69  pred=0.8396  realised=0.1884
      0.9-1.0  n=    6  pred=0.9185  realised=0.6667

  **predicted_p_win_raw → directional**
  n=5377  base_rate=0.5457  Brier=0.2864  REL=0.0391  RES=0.0028  UNC=0.2479  BSS=-0.1553  AUC=0.4980
      0.0-0.1  n=   25  pred=0.0847  realised=0.6400
      0.1-0.2  n=  399  pred=0.1565  realised=0.6090
      0.2-0.3  n=  512  pred=0.2542  realised=0.5938
      0.3-0.4  n= 1018  pred=0.3585  realised=0.4489
      0.4-0.5  n= 1503  pred=0.4551  realised=0.5662
      0.5-0.6  n=  726  pred=0.5469  realised=0.5579
      0.6-0.7  n=  556  pred=0.6465  realised=0.5126
      0.7-0.8  n=  431  pred=0.7470  realised=0.5870
      0.8-0.9  n=  203  pred=0.8371  realised=0.5764
      0.9-1.0  n=    4  pred=0.9117  realised=0.7500

  **predicted_p_win_raw → bracket**
  n=2510  base_rate=0.3171  Brier=0.2866  REL=0.0791  RES=0.0097  UNC=0.2166  BSS=-0.3234  AUC=0.4492
      0.0-0.1  n=    9  pred=0.0902  realised=0.2222
      0.1-0.2  n=  208  pred=0.1556  realised=0.5577
      0.2-0.3  n=  246  pred=0.2533  realised=0.4065
      0.3-0.4  n=  461  pred=0.3530  realised=0.3167
      0.4-0.5  n=  498  pred=0.4503  realised=0.2068
      0.5-0.6  n=  470  pred=0.5485  realised=0.2617
      0.6-0.7  n=  317  pred=0.6468  realised=0.2997
      0.7-0.8  n=  221  pred=0.7411  realised=0.4027
      0.8-0.9  n=   74  pred=0.8388  realised=0.2432
      0.9-1.0  n=    6  pred=0.9185  realised=0.6667

  **p_win_bracket → bracket**
  n=721  base_rate=0.3440  Brier=0.2732  REL=0.0449  RES=0.0028  UNC=0.2257  BSS=-0.2108  AUC=0.4434
      0.0-0.1  n=  450  pred=0.0747  realised=0.3311
      0.1-0.2  n=   56  pred=0.1075  realised=0.2857
      0.2-0.3  n=   19  pred=0.2627  realised=0.2632
      0.3-0.4  n=   54  pred=0.3417  realised=0.3148
      0.4-0.5  n=  102  pred=0.4378  realised=0.3922
      0.5-0.6  n=    8  pred=0.5238  realised=0.5000
      0.6-0.7  n=   17  pred=0.6251  realised=0.5294
      0.7-0.8  n=   15  pred=0.7333  realised=0.5333

  **p_win_dir → directional**
  n=1231  base_rate=0.6409  Brier=0.3290  REL=0.1015  RES=0.0027  UNC=0.2301  BSS=-0.4296  AUC=0.4799
      0.0-0.1  n=    9  pred=0.0845  realised=0.6667
      0.1-0.2  n=  208  pred=0.1517  realised=0.6683
      0.2-0.3  n=  212  pred=0.2585  realised=0.6698
      0.3-0.4  n=  195  pred=0.3512  realised=0.6000
      0.4-0.5  n=  212  pred=0.4516  realised=0.6226
      0.5-0.6  n=  224  pred=0.5439  realised=0.7054
      0.6-0.7  n=   90  pred=0.6438  realised=0.5889
      0.7-0.8  n=   58  pred=0.7405  realised=0.5517
      0.8-0.9  n=   23  pred=0.8561  realised=0.4348

### Calibration stratified by urgency


  high-urgency n=619, on-track n=5483
  **p_win → directional** [high-urgency batch]
  n=543  base_rate=0.3315  Brier=0.2756  REL=0.0557  RES=0.0043  UNC=0.2216  BSS=-0.2437  AUC=0.4704

  high-urgency n=619, on-track n=5483
  **p_win → directional** [on-track batch]
  n=4834  base_rate=0.5697  Brier=0.2864  REL=0.0410  RES=0.0016  UNC=0.2451  BSS=-0.1683  AUC=0.4974

---

## US500 — n=6107 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6107
  batches: 1222
  M_i mean=0.7035  min=0.0000  max=1.5000
  drift mean=1.1726  min=0.0000  max=2.5000
  high-urgency trades: 630 (10.3%)

### Brier decomposition


  **p_win → directional**
  n=5261  base_rate=0.5045  Brier=0.3110  REL=0.0650  RES=0.0043  UNC=0.2500  BSS=-0.2443  AUC=0.4539
      0.0-0.1  n=  124  pred=0.0795  realised=0.6774
      0.1-0.2  n=  834  pred=0.1591  realised=0.5995
      0.2-0.3  n=  980  pred=0.2575  realised=0.4592
      0.3-0.4  n= 1173  pred=0.3511  realised=0.5311
      0.4-0.5  n=  797  pred=0.4486  realised=0.5082
      0.5-0.6  n=  761  pred=0.5280  realised=0.4126
      0.6-0.7  n=  221  pred=0.6502  realised=0.4344
      0.7-0.8  n=  215  pred=0.7467  realised=0.4791
      0.8-0.9  n=  130  pred=0.8400  realised=0.4692
      0.9-1.0  n=   26  pred=0.9202  realised=0.6923

  **p_win → bracket**
  n=2250  base_rate=0.2480  Brier=0.2646  REL=0.0852  RES=0.0081  UNC=0.1865  BSS=-0.4191  AUC=0.5120
      0.0-0.1  n=   68  pred=0.0808  realised=0.5735
      0.1-0.2  n=  200  pred=0.1538  realised=0.3550
      0.2-0.3  n=  292  pred=0.2529  realised=0.1336
      0.3-0.4  n=  337  pred=0.3471  realised=0.1632
      0.4-0.5  n=  404  pred=0.4526  realised=0.2525
      0.5-0.6  n=  473  pred=0.5419  realised=0.2262
      0.6-0.7  n=  152  pred=0.6527  realised=0.3289
      0.7-0.8  n=  268  pred=0.7488  realised=0.2799
      0.8-0.9  n=   54  pred=0.8332  realised=0.3704
      0.9-1.0  n=    2  pred=0.9164  realised=0.0000

  **predicted_p_win_raw → directional**
  n=5261  base_rate=0.5045  Brier=0.3246  REL=0.0790  RES=0.0051  UNC=0.2500  BSS=-0.2985  AUC=0.4586
      0.0-0.1  n=  153  pred=0.0788  realised=0.6275
      0.1-0.2  n= 1142  pred=0.1584  realised=0.5841
      0.2-0.3  n= 1143  pred=0.2529  realised=0.4269
      0.3-0.4  n= 1030  pred=0.3522  realised=0.5553
      0.4-0.5  n=  670  pred=0.4496  realised=0.5209
      0.5-0.6  n=  344  pred=0.5412  realised=0.4302
      0.6-0.7  n=  290  pred=0.6539  realised=0.3897
      0.7-0.8  n=  296  pred=0.7472  realised=0.4324
      0.8-0.9  n=  149  pred=0.8410  realised=0.4966
      0.9-1.0  n=   44  pred=0.9206  realised=0.4318

  **predicted_p_win_raw → bracket**
  n=2250  base_rate=0.2480  Brier=0.2796  REL=0.1014  RES=0.0108  UNC=0.1865  BSS=-0.4991  AUC=0.4975
      0.0-0.1  n=   81  pred=0.0791  realised=0.5802
      0.1-0.2  n=  238  pred=0.1545  realised=0.3361
      0.2-0.3  n=  333  pred=0.2523  realised=0.1381
      0.3-0.4  n=  351  pred=0.3447  realised=0.1738
      0.4-0.5  n=  298  pred=0.4499  realised=0.2416
      0.5-0.6  n=  282  pred=0.5508  realised=0.1560
      0.6-0.7  n=  205  pred=0.6571  realised=0.3951
      0.7-0.8  n=  365  pred=0.7546  realised=0.2658
      0.8-0.9  n=   95  pred=0.8329  realised=0.3158
      0.9-1.0  n=    2  pred=0.9164  realised=0.0000

  **p_win_bracket → bracket**
  n=1073  base_rate=0.2945  Brier=0.2650  REL=0.0895  RES=0.0309  UNC=0.2078  BSS=-0.2753  AUC=0.4750
      0.0-0.1  n=  451  pred=0.0273  realised=0.3947
      0.1-0.2  n=  311  pred=0.1547  realised=0.1415
      0.2-0.3  n=   35  pred=0.2314  realised=0.8857
      0.3-0.4  n=   61  pred=0.3794  realised=0.1639
      0.4-0.5  n=  157  pred=0.4615  realised=0.1338
      0.5-0.6  n=   55  pred=0.5055  realised=0.5636
      0.6-0.7  n=    3  pred=0.6000  realised=0.3333

  **p_win_dir → directional**
  n=1112  base_rate=0.4595  Brier=0.2880  REL=0.0426  RES=0.0034  UNC=0.2484  BSS=-0.1597  AUC=0.5147
      0.0-0.1  n=    4  pred=0.0825  realised=0.2500
      0.1-0.2  n=   62  pred=0.1651  realised=0.4032
      0.2-0.3  n=  139  pred=0.2519  realised=0.5108
      0.3-0.4  n=  142  pred=0.3467  realised=0.4930
      0.4-0.5  n=  154  pred=0.4483  realised=0.4351
      0.5-0.6  n=  149  pred=0.5441  realised=0.3691
      0.6-0.7  n=  178  pred=0.6532  realised=0.4326
      0.7-0.8  n=  219  pred=0.7453  realised=0.4840
      0.8-0.9  n=   65  pred=0.8214  realised=0.6000

### Calibration stratified by urgency


  high-urgency n=630, on-track n=5477
  **p_win → directional** [high-urgency batch]
  n=449  base_rate=0.3452  Brier=0.2806  REL=0.0735  RES=0.0209  UNC=0.2260  BSS=-0.2413  AUC=0.3850

  high-urgency n=630, on-track n=5477
  **p_win → directional** [on-track batch]
  n=4812  base_rate=0.5193  Brier=0.3139  REL=0.0677  RES=0.0037  UNC=0.2496  BSS=-0.2574  AUC=0.4611

---

## USDCAD — n=6114 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6114
  batches: 1223
  M_i mean=0.6686  min=0.0000  max=1.5000
  drift mean=1.1143  min=0.0000  max=2.5000
  high-urgency trades: 411 (6.7%)

### Brier decomposition


  **p_win → directional**
  n=5420  base_rate=0.5153  Brier=0.3049  REL=0.0594  RES=0.0044  UNC=0.2498  BSS=-0.2209  AUC=0.4636
      0.0-0.1  n=   16  pred=0.0853  realised=0.3125
      0.1-0.2  n=  253  pred=0.1576  realised=0.4822
      0.2-0.3  n=  958  pred=0.2599  realised=0.5595
      0.3-0.4  n= 1178  pred=0.3443  realised=0.5204
      0.4-0.5  n= 1128  pred=0.4511  realised=0.5789
      0.5-0.6  n=  675  pred=0.5271  realised=0.5259
      0.6-0.7  n=  294  pred=0.6528  realised=0.5170
      0.7-0.8  n=  573  pred=0.7572  realised=0.3682
      0.8-0.9  n=  319  pred=0.8376  realised=0.4075
      0.9-1.0  n=   26  pred=0.9258  realised=0.6154

  **p_win → bracket**
  n=1467  base_rate=0.2256  Brier=0.2711  REL=0.1081  RES=0.0123  UNC=0.1747  BSS=-0.5519  AUC=0.5871
      0.0-0.1  n=    5  pred=0.0795  realised=0.0000
      0.1-0.2  n=   77  pred=0.1533  realised=0.1299
      0.2-0.3  n=  158  pred=0.2558  realised=0.1835
      0.3-0.4  n=  261  pred=0.3470  realised=0.1303
      0.4-0.5  n=  329  pred=0.4621  realised=0.1641
      0.5-0.6  n=  260  pred=0.5404  realised=0.4346
      0.6-0.7  n=   96  pred=0.6507  realised=0.3125
      0.7-0.8  n=  147  pred=0.7544  realised=0.2653
      0.8-0.9  n=  103  pred=0.8437  realised=0.1942
      0.9-1.0  n=   31  pred=0.9385  realised=0.0645

  **predicted_p_win_raw → directional**
  n=5420  base_rate=0.5153  Brier=0.3138  REL=0.0673  RES=0.0037  UNC=0.2498  BSS=-0.2563  AUC=0.4665
      0.0-0.1  n=   37  pred=0.0870  realised=0.4054
      0.1-0.2  n=  404  pred=0.1560  realised=0.4901
      0.2-0.3  n= 1249  pred=0.2582  realised=0.5508
      0.3-0.4  n= 1050  pred=0.3450  realised=0.5457
      0.4-0.5  n= 1012  pred=0.4490  realised=0.5682
      0.5-0.6  n=  426  pred=0.5430  realised=0.5117
      0.6-0.7  n=  281  pred=0.6496  realised=0.4947
      0.7-0.8  n=  597  pred=0.7578  realised=0.3819
      0.8-0.9  n=  338  pred=0.8378  realised=0.4231
      0.9-1.0  n=   26  pred=0.9258  realised=0.6154

  **predicted_p_win_raw → bracket**
  n=1467  base_rate=0.2256  Brier=0.2694  REL=0.1029  RES=0.0094  UNC=0.1747  BSS=-0.5416  AUC=0.6102
      0.0-0.1  n=    8  pred=0.0789  realised=0.0000
      0.1-0.2  n=   95  pred=0.1513  realised=0.1368
      0.2-0.3  n=  219  pred=0.2621  realised=0.1370
      0.3-0.4  n=  197  pred=0.3442  realised=0.1624
      0.4-0.5  n=  314  pred=0.4589  realised=0.1656
      0.5-0.6  n=  166  pred=0.5459  realised=0.3494
      0.6-0.7  n=  121  pred=0.6460  realised=0.3967
      0.7-0.8  n=  165  pred=0.7553  realised=0.2848
      0.8-0.9  n=  151  pred=0.8384  realised=0.3245
      0.9-1.0  n=   31  pred=0.9385  realised=0.0645

  **p_win_bracket → bracket**
  n=366  base_rate=0.3033  Brier=0.2260  REL=0.0439  RES=0.0330  UNC=0.2113  BSS=-0.0696  AUC=0.6399
      0.0-0.1  n=  115  pred=0.0209  realised=0.0957
      0.1-0.2  n=   60  pred=0.1675  realised=0.4167
      0.2-0.3  n=   12  pred=0.2343  realised=0.0833
      0.3-0.4  n=    3  pred=0.3215  realised=0.6667
      0.4-0.5  n=   47  pred=0.4693  realised=0.3830
      0.5-0.6  n=   33  pred=0.5000  realised=0.5152
      0.6-0.7  n=   48  pred=0.6215  realised=0.2292
      0.7-0.8  n=   46  pred=0.7639  realised=0.5652
      0.8-0.9  n=    2  pred=0.8667  realised=0.0000

  **p_win_dir → directional**
  n=1273  base_rate=0.3888  Brier=0.2687  REL=0.0493  RES=0.0190  UNC=0.2376  BSS=-0.1306  AUC=0.5826
      0.0-0.1  n=   23  pred=0.0883  realised=0.2609
      0.1-0.2  n=  172  pred=0.1505  realised=0.3372
      0.2-0.3  n=  283  pred=0.2451  realised=0.2438
      0.3-0.4  n=  125  pred=0.3483  realised=0.4160
      0.4-0.5  n=  105  pred=0.4492  realised=0.5048
      0.5-0.6  n=  130  pred=0.5506  realised=0.4692
      0.6-0.7  n=   90  pred=0.6470  realised=0.6222
      0.7-0.8  n=  267  pred=0.7583  realised=0.3146
      0.8-0.9  n=   62  pred=0.8502  realised=0.6935
      0.9-1.0  n=   16  pred=0.9298  realised=0.8125

### Calibration stratified by urgency


  high-urgency n=411, on-track n=5703
  **p_win → directional** [high-urgency batch]
  n=302  base_rate=0.2417  Brier=0.2962  REL=0.1281  RES=0.0161  UNC=0.1833  BSS=-0.6160  AUC=0.3659

  high-urgency n=411, on-track n=5703
  **p_win → directional** [on-track batch]
  n=5118  base_rate=0.5315  Brier=0.3055  REL=0.0609  RES=0.0045  UNC=0.2490  BSS=-0.2267  AUC=0.4670

---

## USDCHF — n=6113 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6113
  batches: 1223
  M_i mean=0.6650  min=0.0000  max=1.5000
  drift mean=1.1083  min=0.0000  max=2.5000
  high-urgency trades: 374 (6.1%)

### Brier decomposition


  **p_win → directional**
  n=5372  base_rate=0.5886  Brier=0.2915  REL=0.0528  RES=0.0043  UNC=0.2421  BSS=-0.2039  AUC=0.5563
      0.0-0.1  n=  207  pred=0.0863  realised=0.5024
      0.1-0.2  n=  715  pred=0.1562  realised=0.4993
      0.2-0.3  n=  720  pred=0.2575  realised=0.5611
      0.3-0.4  n=  642  pred=0.3489  realised=0.5280
      0.4-0.5  n=  977  pred=0.4593  realised=0.6111
      0.5-0.6  n=  972  pred=0.5234  realised=0.6759
      0.6-0.7  n=  293  pred=0.6510  realised=0.6348
      0.7-0.8  n=  496  pred=0.7555  realised=0.5927
      0.8-0.9  n=  336  pred=0.8320  realised=0.6607
      0.9-1.0  n=   14  pred=0.9167  realised=0.1429

  **p_win → bracket**
  n=1415  base_rate=0.2431  Brier=0.2930  REL=0.1237  RES=0.0145  UNC=0.1840  BSS=-0.5925  AUC=0.4826
      0.0-0.1  n=   10  pred=0.0789  realised=0.0000
      0.1-0.2  n=  187  pred=0.1634  realised=0.2246
      0.2-0.3  n=  272  pred=0.2580  realised=0.1801
      0.3-0.4  n=  170  pred=0.3464  realised=0.2647
      0.4-0.5  n=  273  pred=0.4573  realised=0.4286
      0.5-0.6  n=  152  pred=0.5237  realised=0.3289
      0.6-0.7  n=   69  pred=0.6545  realised=0.1884
      0.7-0.8  n=  129  pred=0.7509  realised=0.1938
      0.8-0.9  n=  131  pred=0.8579  realised=0.0153
      0.9-1.0  n=   22  pred=0.9370  realised=0.0455

  **predicted_p_win_raw → directional**
  n=5372  base_rate=0.5886  Brier=0.3183  REL=0.0800  RES=0.0040  UNC=0.2421  BSS=-0.3143  AUC=0.5320
      0.0-0.1  n=  553  pred=0.0797  realised=0.6618
      0.1-0.2  n=  787  pred=0.1586  realised=0.4727
      0.2-0.3  n=  804  pred=0.2552  realised=0.5572
      0.3-0.4  n=  697  pred=0.3491  realised=0.5653
      0.4-0.5  n=  852  pred=0.4574  realised=0.6373
      0.5-0.6  n=  368  pred=0.5452  realised=0.5897
      0.6-0.7  n=  325  pred=0.6522  realised=0.6338
      0.7-0.8  n=  581  pred=0.7548  realised=0.6041
      0.8-0.9  n=  384  pred=0.8321  realised=0.6719
      0.9-1.0  n=   21  pred=0.9227  realised=0.3333

  **predicted_p_win_raw → bracket**
  n=1415  base_rate=0.2431  Brier=0.2972  REL=0.1295  RES=0.0162  UNC=0.1840  BSS=-0.6153  AUC=0.4960
      0.0-0.1  n=   15  pred=0.0823  realised=0.0667
      0.1-0.2  n=  284  pred=0.1669  realised=0.1901
      0.2-0.3  n=  214  pred=0.2529  realised=0.1869
      0.3-0.4  n=  187  pred=0.3470  realised=0.3102
      0.4-0.5  n=  227  pred=0.4550  realised=0.4714
      0.5-0.6  n=   85  pred=0.5433  realised=0.2588
      0.6-0.7  n=   81  pred=0.6536  realised=0.2469
      0.7-0.8  n=  143  pred=0.7506  realised=0.2587
      0.8-0.9  n=  157  pred=0.8607  realised=0.0255
      0.9-1.0  n=   22  pred=0.9370  realised=0.0455

  **p_win_bracket → bracket**
  n=519  base_rate=0.2119  Brier=0.2023  REL=0.0580  RES=0.0240  UNC=0.1670  BSS=-0.2115  AUC=0.6661
      0.0-0.1  n=   86  pred=0.0062  realised=0.0000
      0.1-0.2  n=   19  pred=0.1537  realised=0.2632
      0.2-0.3  n=   87  pred=0.2590  realised=0.0460
      0.3-0.4  n=   54  pred=0.3421  realised=0.4444
      0.4-0.5  n=   57  pred=0.4540  realised=0.3684
      0.5-0.6  n=  146  pred=0.5209  realised=0.2055
      0.6-0.7  n=   33  pred=0.6000  realised=0.3939
      0.8-0.9  n=   37  pred=0.8444  realised=0.3514

  **p_win_dir → directional**
  n=1185  base_rate=0.5072  Brier=0.2498  REL=0.0236  RES=0.0222  UNC=0.2499  BSS=+0.0005  AUC=0.6645
      0.0-0.1  n=   22  pred=0.0859  realised=0.1364
      0.1-0.2  n=  244  pred=0.1681  realised=0.2828
      0.2-0.3  n=  180  pred=0.2507  realised=0.4111
      0.3-0.4  n=  202  pred=0.3537  realised=0.5941
      0.4-0.5  n=  187  pred=0.4507  realised=0.6096
      0.5-0.6  n=   84  pred=0.5509  realised=0.5714
      0.6-0.7  n=   64  pred=0.6574  realised=0.6250
      0.7-0.8  n=  123  pred=0.7568  realised=0.6504
      0.8-0.9  n=   72  pred=0.8264  realised=0.6667
      0.9-1.0  n=    7  pred=0.9346  realised=0.7143

### Calibration stratified by urgency


  high-urgency n=374, on-track n=5739
  **p_win → directional** [high-urgency batch]
  n=278  base_rate=0.3489  Brier=0.3561  REL=0.1567  RES=0.0294  UNC=0.2272  BSS=-0.5674  AUC=0.3198

  high-urgency n=374, on-track n=5739
  **p_win → directional** [on-track batch]
  n=5094  base_rate=0.6017  Brier=0.2880  REL=0.0524  RES=0.0048  UNC=0.2397  BSS=-0.2017  AUC=0.5684

---

## USDJPY — n=6115 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6115
  batches: 1223
  M_i mean=0.6992  min=0.0000  max=1.5000
  drift mean=1.1654  min=0.0000  max=2.5000
  high-urgency trades: 648 (10.6%)

### Brier decomposition


  **p_win → directional**
  n=5310  base_rate=0.4942  Brier=0.2879  REL=0.0432  RES=0.0051  UNC=0.2500  BSS=-0.1517  AUC=0.5012
      0.0-0.1  n=   41  pred=0.0859  realised=0.5854
      0.1-0.2  n=  429  pred=0.1547  realised=0.5245
      0.2-0.3  n=  668  pred=0.2489  realised=0.5973
      0.3-0.4  n= 1145  pred=0.3454  realised=0.3904
      0.4-0.5  n= 1026  pred=0.4547  realised=0.4912
      0.5-0.6  n=  677  pred=0.5467  realised=0.5347
      0.6-0.7  n=  637  pred=0.6448  realised=0.5495
      0.7-0.8  n=  500  pred=0.7445  realised=0.4260
      0.8-0.9  n=  157  pred=0.8410  realised=0.5032
      0.9-1.0  n=   30  pred=0.9174  realised=0.7000

  **p_win → bracket**
  n=2610  base_rate=0.3054  Brier=0.2605  REL=0.0625  RES=0.0121  UNC=0.2121  BSS=-0.2281  AUC=0.5561
      0.0-0.1  n=   17  pred=0.0833  realised=0.4706
      0.1-0.2  n=  256  pred=0.1454  realised=0.2656
      0.2-0.3  n=  254  pred=0.2506  realised=0.2323
      0.3-0.4  n=  743  pred=0.3425  realised=0.2194
      0.4-0.5  n=  608  pred=0.4520  realised=0.4457
      0.5-0.6  n=  243  pred=0.5409  realised=0.3868
      0.6-0.7  n=  175  pred=0.6499  realised=0.4229
      0.7-0.8  n=  141  pred=0.7452  realised=0.3050
      0.8-0.9  n=  133  pred=0.8529  realised=0.1278
      0.9-1.0  n=   40  pred=0.9144  realised=0.0000

  **predicted_p_win_raw → directional**
  n=5310  base_rate=0.4942  Brier=0.2916  REL=0.0465  RES=0.0049  UNC=0.2500  BSS=-0.1665  AUC=0.5003
      0.0-0.1  n=   46  pred=0.0836  realised=0.5870
      0.1-0.2  n=  440  pred=0.1541  realised=0.5159
      0.2-0.3  n=  703  pred=0.2488  realised=0.5846
      0.3-0.4  n= 1162  pred=0.3414  realised=0.3941
      0.4-0.5  n=  963  pred=0.4494  realised=0.4984
      0.5-0.6  n=  621  pred=0.5497  realised=0.5298
      0.6-0.7  n=  627  pred=0.6449  realised=0.5821
      0.7-0.8  n=  523  pred=0.7450  realised=0.4340
      0.8-0.9  n=  174  pred=0.8429  realised=0.4540
      0.9-1.0  n=   51  pred=0.9190  realised=0.4118

  **predicted_p_win_raw → bracket**
  n=2610  base_rate=0.3054  Brier=0.2647  REL=0.0671  RES=0.0120  UNC=0.2121  BSS=-0.2479  AUC=0.5587
      0.0-0.1  n=   19  pred=0.0839  realised=0.4737
      0.1-0.2  n=  268  pred=0.1453  realised=0.2649
      0.2-0.3  n=  281  pred=0.2507  realised=0.2242
      0.3-0.4  n=  753  pred=0.3362  realised=0.2311
      0.4-0.5  n=  562  pred=0.4415  realised=0.4537
      0.5-0.6  n=  196  pred=0.5484  realised=0.3622
      0.6-0.7  n=  160  pred=0.6521  realised=0.4688
      0.7-0.8  n=  147  pred=0.7458  realised=0.3129
      0.8-0.9  n=  162  pred=0.8537  realised=0.1975
      0.9-1.0  n=   62  pred=0.9165  realised=0.0161

  **p_win_bracket → bracket**
  n=575  base_rate=0.1687  Brier=0.1671  REL=0.0427  RES=0.0143  UNC=0.1402  BSS=-0.1918  AUC=0.6407
      0.0-0.1  n=  103  pred=0.0729  realised=0.1068
      0.1-0.2  n=  365  pred=0.1300  realised=0.1534
      0.2-0.3  n=   19  pred=0.2820  realised=0.3684
      0.3-0.4  n=   10  pred=0.3846  realised=0.2000
      0.4-0.5  n=   25  pred=0.4483  realised=0.6800
      0.7-0.8  n=   53  pred=0.7256  realised=0.0755

  **p_win_dir → directional**
  n=1165  base_rate=0.5528  Brier=0.2894  REL=0.0517  RES=0.0094  UNC=0.2472  BSS=-0.1705  AUC=0.5417
      0.0-0.1  n=   15  pred=0.0831  realised=0.9333
      0.1-0.2  n=  157  pred=0.1475  realised=0.5541
      0.2-0.3  n=  181  pred=0.2503  realised=0.4254
      0.3-0.4  n=  196  pred=0.3523  realised=0.5459
      0.4-0.5  n=  157  pred=0.4474  realised=0.5350
      0.5-0.6  n=  162  pred=0.5573  realised=0.5185
      0.6-0.7  n=  201  pred=0.6359  realised=0.6915
      0.7-0.8  n=   51  pred=0.7536  realised=0.5882
      0.8-0.9  n=   40  pred=0.8443  realised=0.4250
      0.9-1.0  n=    5  pred=0.9299  realised=1.0000

### Calibration stratified by urgency


  high-urgency n=648, on-track n=5467
  **p_win → directional** [high-urgency batch]
  n=499  base_rate=0.3146  Brier=0.2532  REL=0.0618  RES=0.0216  UNC=0.2156  BSS=-0.1740  AUC=0.5580

  high-urgency n=648, on-track n=5467
  **p_win → directional** [on-track batch]
  n=4811  base_rate=0.5128  Brier=0.2915  REL=0.0458  RES=0.0042  UNC=0.2498  BSS=-0.1667  AUC=0.4927

---

## XAGUSD — n=6044 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6044
  batches: 1209
  M_i mean=0.7944  min=0.0000  max=1.5000
  drift mean=1.3240  min=0.0000  max=2.5000
  high-urgency trades: 1359 (22.5%)

### Brier decomposition


  **p_win → directional**
  n=5260  base_rate=0.5120  Brier=0.2977  REL=0.0547  RES=0.0079  UNC=0.2499  BSS=-0.1913  AUC=0.4319
      0.0-0.1  n=   26  pred=0.0840  realised=0.6154
      0.1-0.2  n=  298  pred=0.1660  realised=0.4698
      0.2-0.3  n=  805  pred=0.2534  realised=0.5441
      0.3-0.4  n=  943  pred=0.3500  realised=0.5981
      0.4-0.5  n= 1255  pred=0.4521  realised=0.5705
      0.5-0.6  n= 1149  pred=0.5304  realised=0.4726
      0.6-0.7  n=  518  pred=0.6538  realised=0.4151
      0.7-0.8  n=  212  pred=0.7385  realised=0.2594
      0.8-0.9  n=   54  pred=0.8233  realised=0.1111

  **p_win → bracket**
  n=5374  base_rate=0.3189  Brier=0.2565  REL=0.0552  RES=0.0155  UNC=0.2172  BSS=-0.1809  AUC=0.5034
      0.0-0.1  n=   24  pred=0.0842  realised=0.3750
      0.1-0.2  n=  413  pred=0.1708  realised=0.2857
      0.2-0.3  n=  866  pred=0.2538  realised=0.2344
      0.3-0.4  n=  951  pred=0.3488  realised=0.2702
      0.4-0.5  n= 1271  pred=0.4512  realised=0.5161
      0.5-0.6  n= 1051  pred=0.5309  realised=0.3302
      0.6-0.7  n=  496  pred=0.6548  realised=0.1310
      0.7-0.8  n=  219  pred=0.7396  realised=0.1461
      0.8-0.9  n=   74  pred=0.8315  realised=0.2973
      0.9-1.0  n=    9  pred=0.9142  realised=0.5556

  **predicted_p_win_raw → directional**
  n=5260  base_rate=0.5120  Brier=0.3051  REL=0.0632  RES=0.0080  UNC=0.2499  BSS=-0.2211  AUC=0.4355
      0.0-0.1  n=   28  pred=0.0832  realised=0.6429
      0.1-0.2  n=  403  pred=0.1632  realised=0.4888
      0.2-0.3  n=  924  pred=0.2538  realised=0.5455
      0.3-0.4  n=  990  pred=0.3496  realised=0.6091
      0.4-0.5  n= 1108  pred=0.4499  realised=0.5749
      0.5-0.6  n=  871  pred=0.5394  realised=0.4397
      0.6-0.7  n=  575  pred=0.6535  realised=0.4174
      0.7-0.8  n=  299  pred=0.7442  realised=0.3411
      0.8-0.9  n=   62  pred=0.8271  realised=0.1452

  **predicted_p_win_raw → bracket**
  n=5374  base_rate=0.3189  Brier=0.2627  REL=0.0608  RES=0.0150  UNC=0.2172  BSS=-0.2095  AUC=0.5008
      0.0-0.1  n=   26  pred=0.0833  realised=0.3846
      0.1-0.2  n=  519  pred=0.1674  realised=0.2967
      0.2-0.3  n=  969  pred=0.2539  realised=0.2343
      0.3-0.4  n=  988  pred=0.3489  realised=0.3036
      0.4-0.5  n= 1148  pred=0.4489  realised=0.5087
      0.5-0.6  n=  770  pred=0.5399  realised=0.3818
      0.6-0.7  n=  570  pred=0.6551  realised=0.1404
      0.7-0.8  n=  291  pred=0.7440  realised=0.1271
      0.8-0.9  n=   73  pred=0.8324  realised=0.3014
      0.9-1.0  n=   20  pred=0.9206  realised=0.3000

  **p_win_bracket → bracket**
  n=1339  base_rate=0.2666  Brier=0.2009  REL=0.0228  RES=0.0168  UNC=0.1955  BSS=-0.0272  AUC=0.5851
      0.0-0.1  n=   15  pred=0.0906  realised=0.6000
      0.1-0.2  n=  377  pred=0.1683  realised=0.2785
      0.2-0.3  n=  480  pred=0.2274  realised=0.1375
      0.3-0.4  n=  132  pred=0.3530  realised=0.2803
      0.4-0.5  n=  120  pred=0.4507  realised=0.2583
      0.5-0.6  n=  159  pred=0.5032  realised=0.5220
      0.6-0.7  n=    4  pred=0.6084  realised=0.2500
      0.9-1.0  n=   52  pred=0.9715  realised=0.4808

  **p_win_dir → directional**
  n=1083  base_rate=0.4691  Brier=0.3174  REL=0.0804  RES=0.0124  UNC=0.2490  BSS=-0.2746  AUC=0.4186
      0.0-0.1  n=    9  pred=0.0829  realised=1.0000
      0.1-0.2  n=  106  pred=0.1560  realised=0.6321
      0.2-0.3  n=  141  pred=0.2502  realised=0.4965
      0.3-0.4  n=  141  pred=0.3493  realised=0.5461
      0.4-0.5  n=  185  pred=0.4524  realised=0.4108
      0.5-0.6  n=  134  pred=0.5487  realised=0.4179
      0.6-0.7  n=  274  pred=0.6594  realised=0.4781
      0.7-0.8  n=   55  pred=0.7501  realised=0.3273
      0.8-0.9  n=   38  pred=0.8304  realised=0.1053

### Calibration stratified by urgency


  high-urgency n=1359, on-track n=4685
  **p_win → directional** [high-urgency batch]
  n=1106  base_rate=0.3897  Brier=0.2921  REL=0.0597  RES=0.0072  UNC=0.2378  BSS=-0.2282  AUC=0.4165

  high-urgency n=1359, on-track n=4685
  **p_win → directional** [on-track batch]
  n=4154  base_rate=0.5445  Brier=0.2991  REL=0.0580  RES=0.0079  UNC=0.2480  BSS=-0.2061  AUC=0.4383

---

## XAUAUD — n=6105 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6105
  batches: 1221
  M_i mean=0.8287  min=0.0000  max=1.5000
  drift mean=1.3812  min=0.0000  max=2.5000
  high-urgency trades: 1434 (23.5%)

### Brier decomposition


  **p_win → directional**
  n=5125  base_rate=0.4675  Brier=0.2714  REL=0.0273  RES=0.0053  UNC=0.2489  BSS=-0.0903  AUC=0.4785
      0.0-0.1  n=    9  pred=0.0879  realised=0.5556
      0.1-0.2  n=  160  pred=0.1611  realised=0.5062
      0.2-0.3  n=  551  pred=0.2599  realised=0.6044
      0.3-0.4  n= 1106  pred=0.3482  realised=0.4882
      0.4-0.5  n= 1558  pred=0.4500  realised=0.3838
      0.5-0.6  n= 1118  pred=0.5460  realised=0.4535
      0.6-0.7  n=  437  pred=0.6406  realised=0.5195
      0.7-0.8  n=  141  pred=0.7437  realised=0.5106
      0.8-0.9  n=   44  pred=0.8404  realised=0.7500
      0.9-1.0  n=    1  pred=0.9281  realised=0.0000

  **p_win → bracket**
  n=5242  base_rate=0.2627  Brier=0.2694  REL=0.0818  RES=0.0070  UNC=0.1937  BSS=-0.3908  AUC=0.4194
      0.0-0.1  n=   16  pred=0.0887  realised=0.5625
      0.1-0.2  n=  204  pred=0.1599  realised=0.4167
      0.2-0.3  n=  595  pred=0.2597  realised=0.3277
      0.3-0.4  n= 1072  pred=0.3473  realised=0.3619
      0.4-0.5  n= 1469  pred=0.4485  realised=0.2063
      0.5-0.6  n= 1090  pred=0.5490  realised=0.1706
      0.6-0.7  n=  431  pred=0.6409  realised=0.2552
      0.7-0.8  n=  244  pred=0.7552  realised=0.2746
      0.8-0.9  n=   91  pred=0.8278  realised=0.3736
      0.9-1.0  n=   30  pred=0.9516  realised=0.0000

  **predicted_p_win_raw → directional**
  n=5125  base_rate=0.4675  Brier=0.2729  REL=0.0274  RES=0.0044  UNC=0.2489  BSS=-0.0962  AUC=0.4822
      0.0-0.1  n=   11  pred=0.0893  realised=0.5455
      0.1-0.2  n=  192  pred=0.1626  realised=0.5052
      0.2-0.3  n=  618  pred=0.2589  realised=0.5841
      0.3-0.4  n= 1104  pred=0.3486  realised=0.4674
      0.4-0.5  n= 1459  pred=0.4475  realised=0.3955
      0.5-0.6  n= 1031  pred=0.5480  realised=0.4520
      0.6-0.7  n=  496  pred=0.6429  realised=0.5121
      0.7-0.8  n=  162  pred=0.7436  realised=0.5123
      0.8-0.9  n=   48  pred=0.8392  realised=0.7500
      0.9-1.0  n=    4  pred=0.9377  realised=0.0000

  **predicted_p_win_raw → bracket**
  n=5242  base_rate=0.2627  Brier=0.2751  REL=0.0861  RES=0.0059  UNC=0.1937  BSS=-0.4206  AUC=0.4211
      0.0-0.1  n=   18  pred=0.0895  realised=0.5000
      0.1-0.2  n=  237  pred=0.1610  realised=0.3671
      0.2-0.3  n=  656  pred=0.2587  realised=0.3323
      0.3-0.4  n= 1069  pred=0.3477  realised=0.3527
      0.4-0.5  n= 1376  pred=0.4464  realised=0.2100
      0.5-0.6  n=  974  pred=0.5493  realised=0.1694
      0.6-0.7  n=  481  pred=0.6426  realised=0.2599
      0.7-0.8  n=  245  pred=0.7543  realised=0.2898
      0.8-0.9  n=   99  pred=0.8278  realised=0.2929
      0.9-1.0  n=   87  pred=0.9489  realised=0.0805

  **p_win_bracket → bracket**
  n=849  base_rate=0.2768  Brier=0.2052  REL=0.0147  RES=0.0101  UNC=0.2002  BSS=-0.0253  AUC=0.5969
      0.0-0.1  n=   34  pred=0.0641  realised=0.4118
      0.1-0.2  n=   56  pred=0.1574  realised=0.0000
      0.2-0.3  n=  203  pred=0.2545  realised=0.2020
      0.3-0.4  n=  258  pred=0.3302  realised=0.3178
      0.4-0.5  n=  178  pred=0.4392  realised=0.2753
      0.5-0.6  n=  120  pred=0.5227  realised=0.4083

  **p_win_dir → directional**
  n=1132  base_rate=0.5027  Brier=0.2850  REL=0.0413  RES=0.0071  UNC=0.2500  BSS=-0.1400  AUC=0.4738
      0.0-0.1  n=    7  pred=0.0899  realised=0.7143
      0.1-0.2  n=   85  pred=0.1627  realised=0.4471
      0.2-0.3  n=  182  pred=0.2529  realised=0.5879
      0.3-0.4  n=  241  pred=0.3486  realised=0.5602
      0.4-0.5  n=  303  pred=0.4559  realised=0.4290
      0.5-0.6  n=  178  pred=0.5338  realised=0.3989
      0.6-0.7  n=   78  pred=0.6508  realised=0.6282
      0.7-0.8  n=   42  pred=0.7412  realised=0.6190
      0.8-0.9  n=   16  pred=0.8358  realised=0.5000

### Calibration stratified by urgency


  high-urgency n=1434, on-track n=4671
  **p_win → directional** [high-urgency batch]
  n=1236  base_rate=0.3519  Brier=0.2630  REL=0.0409  RES=0.0075  UNC=0.2281  BSS=-0.1531  AUC=0.4759

  high-urgency n=1434, on-track n=4671
  **p_win → directional** [on-track batch]
  n=3889  base_rate=0.5042  Brier=0.2741  REL=0.0288  RES=0.0050  UNC=0.2500  BSS=-0.0964  AUC=0.4848

---

## XAUEUR — n=6107 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6107
  batches: 1222
  M_i mean=0.8303  min=0.0000  max=1.5000
  drift mean=1.3838  min=0.0000  max=2.5000
  high-urgency trades: 1342 (22.0%)

### Brier decomposition


  **p_win → directional**
  n=5350  base_rate=0.4925  Brier=0.2674  REL=0.0184  RES=0.0013  UNC=0.2499  BSS=-0.0698  AUC=0.5219
      0.0-0.1  n=    7  pred=0.0912  realised=0.5714
      0.1-0.2  n=  214  pred=0.1676  realised=0.5187
      0.2-0.3  n=  582  pred=0.2536  realised=0.4536
      0.3-0.4  n= 1165  pred=0.3594  realised=0.4618
      0.4-0.5  n= 1559  pred=0.4534  realised=0.4958
      0.5-0.6  n= 1275  pred=0.5379  realised=0.5349
      0.6-0.7  n=  383  pred=0.6399  realised=0.5170
      0.7-0.8  n=  106  pred=0.7448  realised=0.4340
      0.8-0.9  n=   55  pred=0.8360  realised=0.3091
      0.9-1.0  n=    4  pred=0.9221  realised=0.5000

  **p_win → bracket**
  n=4400  base_rate=0.1934  Brier=0.2487  REL=0.0933  RES=0.0021  UNC=0.1560  BSS=-0.5945  AUC=0.4277
      0.0-0.1  n=    9  pred=0.0922  realised=0.3333
      0.1-0.2  n=  179  pred=0.1693  realised=0.2961
      0.2-0.3  n=  561  pred=0.2521  realised=0.2282
      0.3-0.4  n= 1007  pred=0.3588  realised=0.2363
      0.4-0.5  n= 1358  pred=0.4507  realised=0.1694
      0.5-0.6  n=  849  pred=0.5388  realised=0.1437
      0.6-0.7  n=  227  pred=0.6384  realised=0.2070
      0.7-0.8  n=   84  pred=0.7466  realised=0.2262
      0.8-0.9  n=   50  pred=0.8382  realised=0.1400
      0.9-1.0  n=   76  pred=0.9573  realised=0.0526

  **predicted_p_win_raw → directional**
  n=5350  base_rate=0.4925  Brier=0.2764  REL=0.0279  RES=0.0013  UNC=0.2499  BSS=-0.1058  AUC=0.5054
      0.0-0.1  n=   10  pred=0.0907  realised=0.7000
      0.1-0.2  n=  337  pred=0.1650  realised=0.5608
      0.2-0.3  n=  794  pred=0.2520  realised=0.4584
      0.3-0.4  n= 1206  pred=0.3597  realised=0.4809
      0.4-0.5  n= 1349  pred=0.4542  realised=0.4930
      0.5-0.6  n= 1064  pred=0.5464  realised=0.5197
      0.6-0.7  n=  408  pred=0.6417  realised=0.5098
      0.7-0.8  n=  117  pred=0.7423  realised=0.4274
      0.8-0.9  n=   60  pred=0.8363  realised=0.2833
      0.9-1.0  n=    5  pred=0.9180  realised=0.4000

  **predicted_p_win_raw → bracket**
  n=4400  base_rate=0.1934  Brier=0.2509  REL=0.0968  RES=0.0036  UNC=0.1560  BSS=-0.6084  AUC=0.3968
      0.0-0.1  n=   12  pred=0.0908  realised=0.2500
      0.1-0.2  n=  273  pred=0.1667  realised=0.3223
      0.2-0.3  n=  708  pred=0.2477  realised=0.2599
      0.3-0.4  n= 1088  pred=0.3597  realised=0.2197
      0.4-0.5  n= 1120  pred=0.4511  realised=0.1491
      0.5-0.6  n=  726  pred=0.5463  realised=0.1253
      0.6-0.7  n=  249  pred=0.6418  realised=0.1928
      0.7-0.8  n=   91  pred=0.7440  realised=0.2198
      0.8-0.9  n=   56  pred=0.8381  realised=0.1250
      0.9-1.0  n=   77  pred=0.9566  realised=0.0519

  **p_win_bracket → bracket**
  n=909  base_rate=0.1881  Brier=0.1338  REL=0.0035  RES=0.0234  UNC=0.1527  BSS=+0.1241  AUC=0.7001
      0.0-0.1  n=  331  pred=0.0864  realised=0.0997
      0.1-0.2  n=  364  pred=0.1464  realised=0.1099
      0.2-0.3  n=   37  pred=0.2857  realised=0.5405
      0.3-0.4  n=   75  pred=0.3293  realised=0.3867
      0.4-0.5  n=   65  pred=0.4444  realised=0.4462
      0.5-0.6  n=   37  pred=0.5605  realised=0.5405

  **p_win_dir → directional**
  n=1240  base_rate=0.4347  Brier=0.2853  REL=0.0538  RES=0.0147  UNC=0.2457  BSS=-0.1610  AUC=0.4871
      0.0-0.1  n=    3  pred=0.0931  realised=0.6667
      0.1-0.2  n=   80  pred=0.1632  realised=0.5500
      0.2-0.3  n=  247  pred=0.2453  realised=0.3441
      0.3-0.4  n=  164  pred=0.3516  realised=0.5061
      0.4-0.5  n=  190  pred=0.4536  realised=0.3579
      0.5-0.6  n=  316  pred=0.5444  realised=0.5759
      0.6-0.7  n=  138  pred=0.6453  realised=0.4058
      0.7-0.8  n=   57  pred=0.7430  realised=0.2281
      0.8-0.9  n=   44  pred=0.8360  realised=0.1364
      0.9-1.0  n=    1  pred=0.9017  realised=0.0000

### Calibration stratified by urgency


  high-urgency n=1342, on-track n=4765
  **p_win → directional** [high-urgency batch]
  n=1210  base_rate=0.3760  Brier=0.2599  REL=0.0254  RES=0.0016  UNC=0.2346  BSS=-0.1078  AUC=0.4819

  high-urgency n=1342, on-track n=4765
  **p_win → directional** [on-track batch]
  n=4140  base_rate=0.5266  Brier=0.2696  REL=0.0225  RES=0.0022  UNC=0.2493  BSS=-0.0813  AUC=0.5313

---

## XAUUSD — n=6103 resolved

### Urgency state (Masaniello M_i)

  trades analysed: 6103
  batches: 1221
  M_i mean=0.8154  min=0.0000  max=1.5000
  drift mean=1.3590  min=0.0000  max=2.5000
  high-urgency trades: 1279 (21.0%)

### Brier decomposition


  **p_win → directional**
  n=5266  base_rate=0.4945  Brier=0.2688  REL=0.0243  RES=0.0055  UNC=0.2500  BSS=-0.0751  AUC=0.5584
      0.0-0.1  n=   85  pred=0.0839  realised=0.6235
      0.1-0.2  n=  440  pred=0.1541  realised=0.4750
      0.2-0.3  n=  815  pred=0.2511  realised=0.3620
      0.3-0.4  n= 1106  pred=0.3480  realised=0.4602
      0.4-0.5  n= 1181  pred=0.4515  realised=0.5301
      0.5-0.6  n=  975  pred=0.5445  realised=0.5887
      0.6-0.7  n=  448  pred=0.6439  realised=0.5335
      0.7-0.8  n=  172  pred=0.7371  realised=0.4419
      0.8-0.9  n=   43  pred=0.8280  realised=0.5349
      0.9-1.0  n=    1  pred=0.9025  realised=0.0000

  **p_win → bracket**
  n=4253  base_rate=0.2055  Brier=0.2517  REL=0.0893  RES=0.0026  UNC=0.1633  BSS=-0.5413  AUC=0.4402
      0.0-0.1  n=   28  pred=0.0861  realised=0.1071
      0.1-0.2  n=  218  pred=0.1583  realised=0.3486
      0.2-0.3  n=  620  pred=0.2496  realised=0.1839
      0.3-0.4  n=  923  pred=0.3471  realised=0.2384
      0.4-0.5  n= 1117  pred=0.4516  realised=0.2238
      0.5-0.6  n=  770  pred=0.5433  realised=0.1714
      0.6-0.7  n=  346  pred=0.6444  realised=0.1561
      0.7-0.8  n=  154  pred=0.7556  realised=0.0909
      0.8-0.9  n=   42  pred=0.8325  realised=0.2143
      0.9-1.0  n=   35  pred=0.9413  realised=0.0571

  **predicted_p_win_raw → directional**
  n=5266  base_rate=0.4945  Brier=0.2691  REL=0.0273  RES=0.0073  UNC=0.2500  BSS=-0.0767  AUC=0.5697
      0.0-0.1  n=  112  pred=0.0829  realised=0.6607
      0.1-0.2  n=  505  pred=0.1569  realised=0.4139
      0.2-0.3  n=  893  pred=0.2516  realised=0.3819
      0.3-0.4  n= 1196  pred=0.3485  realised=0.4574
      0.4-0.5  n=  969  pred=0.4478  realised=0.5377
      0.5-0.6  n=  821  pred=0.5485  realised=0.6273
      0.6-0.7  n=  504  pred=0.6455  realised=0.5516
      0.7-0.8  n=  210  pred=0.7391  realised=0.4333
      0.8-0.9  n=   55  pred=0.8289  realised=0.5091
      0.9-1.0  n=    1  pred=0.9025  realised=0.0000

  **predicted_p_win_raw → bracket**
  n=4253  base_rate=0.2055  Brier=0.2544  REL=0.0923  RES=0.0026  UNC=0.1633  BSS=-0.5582  AUC=0.4447
      0.0-0.1  n=   52  pred=0.0828  realised=0.1346
      0.1-0.2  n=  273  pred=0.1623  realised=0.2857
      0.2-0.3  n=  654  pred=0.2496  realised=0.1804
      0.3-0.4  n= 1018  pred=0.3478  realised=0.2672
      0.4-0.5  n=  957  pred=0.4501  realised=0.1964
      0.5-0.6  n=  658  pred=0.5467  realised=0.1900
      0.6-0.7  n=  375  pred=0.6443  realised=0.1573
      0.7-0.8  n=  132  pred=0.7433  realised=0.1212
      0.8-0.9  n=   53  pred=0.8345  realised=0.1698
      0.9-1.0  n=   81  pred=0.9518  realised=0.0247

  **p_win_bracket → bracket**
  n=965  base_rate=0.1617  Brier=0.1706  REL=0.0463  RES=0.0122  UNC=0.1355  BSS=-0.2585  AUC=0.5965
      0.0-0.1  n=  260  pred=0.0800  realised=0.0962
      0.1-0.2  n=  368  pred=0.1628  realised=0.1060
      0.3-0.4  n=  179  pred=0.3545  realised=0.3575
      0.4-0.5  n=    8  pred=0.4000  realised=0.0000
      0.5-0.6  n=  104  pred=0.5361  realised=0.2692
      0.8-0.9  n=   46  pred=0.8696  realised=0.0000

  **p_win_dir → directional**
  n=1132  base_rate=0.4320  Brier=0.2946  REL=0.0564  RES=0.0075  UNC=0.2454  BSS=-0.2005  AUC=0.4451
      0.0-0.1  n=    7  pred=0.0830  realised=0.5714
      0.1-0.2  n=   80  pred=0.1514  realised=0.6625
      0.2-0.3  n=  142  pred=0.2583  realised=0.4859
      0.3-0.4  n=  264  pred=0.3571  realised=0.3902
      0.4-0.5  n=  275  pred=0.4441  realised=0.4218
      0.5-0.6  n=  130  pred=0.5444  realised=0.4462
      0.6-0.7  n=  112  pred=0.6449  realised=0.4286
      0.7-0.8  n=   82  pred=0.7491  realised=0.2439
      0.8-0.9  n=   39  pred=0.8283  realised=0.4615
      0.9-1.0  n=    1  pred=0.9025  realised=0.0000

### Calibration stratified by urgency


  high-urgency n=1279, on-track n=4824
  **p_win → directional** [high-urgency batch]
  n=1050  base_rate=0.3324  Brier=0.2380  REL=0.0267  RES=0.0105  UNC=0.2219  BSS=-0.0725  AUC=0.5690

  high-urgency n=1279, on-track n=4824
  **p_win → directional** [on-track batch]
  n=4216  base_rate=0.5349  Brier=0.2764  REL=0.0326  RES=0.0051  UNC=0.2488  BSS=-0.1111  AUC=0.5590

---

## Aggregate verdict

  n=74376  base_rate=0.5247  Brier=0.2876  REL=0.0377  RES=0.0002  UNC=0.2494  BSS=-0.1531  AUC=0.4992

  Verdict: no better than a constant (BSS -0.153); no ordering (AUC 0.499)