#!/usr/bin/env python3
"""
Build a stream-ready test clip: a long bed of REAL ambient (non-drone) audio
with a REAL drone flyby mixed in at a known time. Output matches the sensor
pipeline format exactly: 16 kHz mono PCM16.

Sources (real recordings): saraalemadi/DroneAudioDataset
  - drone:   Multiclass_Drone_Audio/{bebop_1|membo_1}/*.wav
  - ambient: Binary_Drone_Audio/unknown/*.wav  (ESC-50-derived environmental)

Emits:
  drone_flyby_test_16k_mono.wav  -- the clip
  ground_truth.json              -- per-second drone-present labels + flyby window
"""
import glob, os, json, random
import numpy as np, soundfile as sf

random.seed(7); np.random.seed(7)
SR = 16000
ROOT = "DroneAudioDataset"
TOTAL_S = 300                 # 5 minutes
T_CPA   = 180.0               # closest point of approach (s)
SEG_S   = 30.0                # drone audible segment length (s)
D0, V   = 25.0, 10.0          # closest distance (m), speed (m/s) -> flyby shape
DRONE   = "bebop_1"           # swap to membo_1, or point elsewhere for DJI

def load1s(path):
    x, sr = sf.read(path, dtype="float32")
    if x.ndim > 1: x = x.mean(1)
    if sr != SR:   # all are 16k, but be safe
        import scipy.signal as ss
        x = ss.resample(x, int(len(x)*SR/sr)).astype("float32")
    return x[:SR] if len(x) >= SR else np.pad(x, (0, SR-len(x)))

def rms(x): return float(np.sqrt(np.mean(x**2)) + 1e-12)

def xfade_sequence(files, n_clips, fade=int(0.2*SR)):
    """Crossfade-join random 1s clips into one continuous stream."""
    hop = SR - fade
    out = np.zeros(n_clips*hop + fade, dtype="float32")
    ramp = np.linspace(0, 1, fade, dtype="float32")
    for i in range(n_clips):
        c = load1s(random.choice(files))
        s = i*hop
        seg = out[s:s+SR]
        if i > 0:
            c[:fade] *= ramp; seg[:fade] *= (1-ramp)
        out[s:s+SR] += c
    return out

print("indexing real source clips ...")
amb_files   = glob.glob(f"{ROOT}/Binary_Drone_Audio/unknown/*.wav")
drone_files = sorted(glob.glob(f"{ROOT}/Multiclass_Drone_Audio/{DRONE}/*.wav"))
print(f"  ambient pool: {len(amb_files)}   drone pool ({DRONE}): {len(drone_files)}")

# --- ambient bed: two overlapping crossfaded layers => dense continuous noise
N = TOTAL_S*SR
clips_per_layer = int(TOTAL_S/0.8)+2
layA = xfade_sequence(amb_files, clips_per_layer)[:N]
layB = xfade_sequence(amb_files, clips_per_layer)[:N]
if len(layA) < N: layA = np.pad(layA,(0,N-len(layA)))
if len(layB) < N: layB = np.pad(layB,(0,N-len(layB)))
bed = 0.6*layA + 0.6*np.roll(layB, SR//2)
bed *= (0.05 / rms(bed))           # set ambient RMS ~ -26 dBFS

# --- real drone flyby: continuous rotor audio + physical distance envelope
seg_n = int(SEG_S*SR)
_need = int(np.ceil(seg_n/(SR-int(0.2*SR)))) + 2
drone = xfade_sequence(drone_files, _need)
drone = drone[:seg_n] if len(drone)>=seg_n else np.pad(drone,(0,seg_n-len(drone)))
t = (np.arange(seg_n)/SR) - SEG_S/2.0          # time relative to CPA
dist = np.sqrt(D0**2 + (V*t)**2)
gain = D0/dist                                  # 1.0 at CPA, falls with distance
taper = np.ones(seg_n); tn=int(3*SR)            # 3s raised-cosine edges
taper[:tn]  = 0.5*(1-np.cos(np.linspace(0,np.pi,tn)))
taper[-tn:] = taper[:tn][::-1]
env = gain*taper
# distance darkening: blend a low-passed copy when far (low gain)
import scipy.signal as ss
b,a = ss.butter(2, 1800/(SR/2))
drone_lp = ss.lfilter(b,a,drone).astype("float32")
g = env/env.max()
drone_shaped = (g*drone + (1-g)*drone_lp) * env
drone_shaped *= ( (0.05*4.0) / rms(drone_shaped[env>0.5*env.max()]) )  # CPA ~ +12 dB over bed

# --- mix flyby onto the bed at T_CPA
mix = bed.copy()
start = int((T_CPA - SEG_S/2.0)*SR)
mix[start:start+seg_n] += drone_shaped

# --- peak normalize to -1 dBFS, write PCM16
mix *= (10**(-1/20)) / (np.max(np.abs(mix))+1e-9)
sf.write("drone_flyby_test_16k_mono.wav", mix.astype("float32"), SR, subtype="PCM_16")

# --- ground truth: per-second label where envelope > 15% of peak
thr = 0.15*env.max()
active = np.zeros(TOTAL_S, dtype=int)
for s in range(TOTAL_S):
    a0,a1 = s*SR,(s+1)*SR
    seg_env = np.zeros(N); seg_env[start:start+seg_n]=env
    if seg_env[a0:a1].max() > thr: active[s]=1
on = [i for i,v in enumerate(active) if v]
gt = {"sample_rate_hz":SR,"frame_seconds":1,"total_seconds":TOTAL_S,
      "drone_type":DRONE,"flyby_cpa_second":T_CPA,
      "drone_present_window_s":[on[0],on[-1]] if on else [],
      "per_second_label":active.tolist(),
      "notes":"1=drone audibly present. All audio is real (DroneAudioDataset)."}
json.dump(gt, open("ground_truth.json","w"), indent=2)

dur=len(mix)/SR
print(f"\nwrote drone_flyby_test_16k_mono.wav  {dur:.0f}s  {len(mix)} samples  {os.path.getsize('drone_flyby_test_16k_mono.wav')/1e6:.1f} MB")
print(f"flyby audible ~{on[0]}s..{on[-1]}s, peak (CPA) at {T_CPA:.0f}s")
print(f"ambient RMS {20*np.log10(rms(bed)):.1f} dBFS, CPA drone ~+12 dB over bed")
