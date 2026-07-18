philyron1@alpha1:~/vibration_monitoring $ python3 src/vibration_monitor.py
SW_RESET: ['0x1', '0x0', '0x51', '0xbd']
MODE1: ['0x3', '0x0', '0x0', '0x7d']
RS after startup: 01 (expect 01)

Calibrating baseline... keep sensor still for a moment
Baseline -> X:+0.103g Y:+0.987g Z:-0.096g

Sampling at 100 Hz, FFT window = 256 samples (~2.6s per window)
Raw log     -> /home/philyron1/vibration_monitoring/data/raw/raw_vibration_log.csv
Metrics log -> /home/philyron1/vibration_monitoring/data/metrics/vibration_metrics.csv
Press Ctrl+C to stop.

[1784410121.3] window processed | combined RMS=0.000g sources=[none] | health=100.0 (OK)
[1784410123.9] window processed | combined RMS=0.012g sources=[0.4Hz] | health=75.0 (WARNING)
[1784410126.4] window processed | combined RMS=0.005g sources=[0.4Hz] | health=52.0 (ABNORMAL - inspect)
[1784410129.0] window processed | combined RMS=0.000g sources=[none] | health=54.1 (ABNORMAL - inspect)
[1784410131.5] window processed | combined RMS=0.007g sources=[0.4Hz] | health=31.1 (ABNORMAL - inspect)
[1784410134.1] window processed | combined RMS=0.010g sources=[14.5Hz, 1.6Hz, 7.8Hz] | health=8.2 (CRITICAL - fix needed)
[1784410136.7] window processed | combined RMS=0.013g sources=[0.4Hz, 21.1Hz, 8.2Hz] | health=0.0 (CRITICAL - fix needed)
[1784410138.4] !! INSTANT SPIKE !! magnitude=0.361g (z=36.1) -> health=0.0 (CRITICAL - fix needed)
[1784410139.2] window processed | combined RMS=0.027g sources=[0.4Hz, 29.7Hz, 49.2Hz] | health=0.0 (CRITICAL - fix needed)
[1784410139.5] !! INSTANT SPIKE !! magnitude=0.204g (z=7.7) -> health=0.0 (CRITICAL - fix needed)
[1784410141.7] !! INSTANT SPIKE !! magnitude=0.153g (z=15.3) -> health=0.0 (CRITICAL - fix needed)
[1784410141.8] window processed | combined RMS=0.029g sources=[0.4Hz, 5.9Hz, 10.2Hz] | health=0.0 (CRITICAL - fix needed)
[1784410142.0] !! INSTANT SPIKE !! magnitude=0.150g (z=6.1) -> health=0.0 (CRITICAL - fix needed)
[1784410144.3] window processed | combined RMS=0.017g sources=[2.0Hz, 46.5Hz, 48.8Hz] | health=0.0 (CRITICAL - fix needed)
[1784410146.9] window processed | combined RMS=0.008g sources=[0.4Hz, 13.7Hz, 26.2Hz] | health=0.0 (CRITICAL - fix needed)
[1784410149.5] window processed | combined RMS=0.006g sources=[0.4Hz, 43.8Hz] | health=0.0 (CRITICAL - fix needed)
[1784410152.0] window processed | combined RMS=0.000g sources=[none] | health=2.0 (CRITICAL - fix needed)
[1784410154.6] window processed | combined RMS=0.004g sources=[0.4Hz, 27.7Hz] | health=0.0 (CRITICAL - fix needed)
[1784410157.1] window processed | combined RMS=0.007g sources=[3.1Hz, 6.2Hz, 33.6Hz] | health=0.0 (CRITICAL - fix needed)
[1784410159.7] window processed | combined RMS=0.005g sources=[1.6Hz, 6.6Hz, 45.3Hz] | health=0.0 (CRITICAL - fix needed)
[1784410162.3] window processed | combined RMS=0.000g sources=[none] | health=2.0 (CRITICAL - fix needed)
[1784410164.8] window processed | combined RMS=0.000g sources=[none] | health=4.1 (CRITICAL - fix needed)
[1784410167.4] window processed | combined RMS=0.000g sources=[none] | health=6.1 (CRITICAL - fix needed)
[1784410169.9] window processed | combined RMS=0.000g sources=[none] | health=8.2 (CRITICAL - fix needed)
[1784410172.5] window processed | combined RMS=0.000g sources=[none] | health=10.2 (CRITICAL - fix needed)
