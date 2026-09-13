# Hardware

HomeBrain is a **household appliance** (NAS + home + vault + local agent), not an
AI mini-PC. Do not compete with Minisforum / GEEKOM / DGX Spark on unified
memory. Sell a quiet Mini-ITX box that sits on a table.

Two machines:

| | Role | GPU |
|---|---|---|
| **Production** | Lab / daily box. Measured in [BENCHMARKS.md](BENCHMARKS.md). | RX 9060 XT 16 GB (Vulkan / RADV) |
| **Demo** | One unit for product video and photos. This BOM. | Arc Pro B60 24 GB |

16 GB discrete VRAM cannot hold DFlash + vision with Glimmer. The demo unit is
the 24 GB discrete path that is actually for sale in Germany. Mini-ITX is the
product shape. Mini-ATX stays in the lab.

The software stack today is AMD-Vulkan-centric. Intel `xe` already maps to
Vulkan in HomeBrain; a Glimmer / B60 flag profile is still owed after the box
exists.

Prices below are **DE street, Sep 2026, incl. 19% MwSt**. They move. SKUs do
not.

---

## Demo BOM

| Part | Buy | SKU | ~€ |
|---|---|---|---|
| GPU | ASRock Arc Pro B60 Creator 24 GB | 2-slot blower, 200 W, 39 mm, 1× 8-pin | 719 |
| CPU | Ryzen 5 5600 BOX | `100-100000927BOX` | 125 |
| Cooler | Noctua NH-L9a-AM4 | 37 mm. Not the boxed Wraith. | 50 |
| Board | ASRock B550M-ITX/ac | 1× M.2, 1 Gbit, no BIOS Flashback | 114 |
| RAM | G.Skill Ripjaws V 32 GB (2×16) DDR4-3600 **CL16** | `F4-3600C16D-32GVKC` | 235 |
| SSD | Lexar NM790 2 TB, no heatsink | `LNM790X002T-RNNNG` | 269 |
| PSU | Corsair SF750 Platinum ATX 3.1 | `CP-9020284-EU` | 163 |
| Case | 3D-printed tabletop, or Fractal Terra / Ridge | SFX only. Well-vented. | 0–220 |

**Parts without a bought case: ~€1,675.** Ridge ~€1,825. Terra ~€1,895.

1 TB (`LNM790X001T-RNNNG`, ~€175) is enough for a film-only disk (OS + one
Glimmer Q4 + a small photo set). 2 TB is the value pick: one M.2 on this
board, TLC, 1,500 TBW, ~€135/TB. The second terabyte is ~€94.

Case fans are optional if the shell is a chimney. The B60 blower is GPU
exhaust. The L9a is 65 W. A single slow 120 mm on the CPU side is the only
fan that earns its keep in a Terra-like split chamber. Two fans at default
RPM add noise.

---

## Do not buy

| Temptation | Why not |
|---|---|
| B65 Creator 32 GB | Same 20 Xe / 200 W / 39 mm. ~€480 more. Only if you spend it. |
| Gunnir B60 BS | Not sold in DE. Landed Newegg is more than the Creator. |
| 7900 XTX / R9700 | Tok/s kings. 355 W or $1,299. Wrong appliance. |
| Kingston NV3, Crucial P310, Lexar EQ790 / NQ790 | QLC. Write cliff ~200 MB/s. Same money as NM790 TLC. |
| Samsung 990/9100 Pro | Same Gen4 TLC class, €20–40 more. |
| be quiet! SFX 450 / SFX-L 600 | Too small, or Gold and always spinning. |
| Cooler Master V SFX Gold | Same € as the SF750, worse fan curve. |
| NH-L12S / AXP90 as the *freedom* cooler | 70 mm / RAM overhang lock the lid. L9a does not. |
| Boxed Wraith Stealth | Loud in Terra / Ridge / a printed slab. |
| Mini-PC (AI X1, Strix Halo, DGX Spark) | iGPU or unified memory. No 16–32 GB **discrete** GDDR at 456 GB/s. |

---

## Build notes

- **Display** goes in the GPU. The 5600 has no iGPU. Board HDMI stays unused.
- **BIOS.** This ASRock has no Flashback. Ask the shop for a 5000-series BIOS
  at checkout. Enable ReBAR / Above 4G.
- **RAM.** Enable DOCP. Confirm 3600 and dual channel, not JEDEC 2133.
- **Cooler.** L9a-AM4, not L9i. PWM-cap it. 42 mm Ripjaws clear it.
- **PSU.** True SFX, not SFX-L, in Terra / Ridge. Zero RPM to ~300 W — this
  box peaks around that. The 12V-2×6 cable stays in the bag unless a B65
  arrives later. GPU uses the 8-pin.
- **3D-printed lid.** Chamber = cooler height + 8–10 mm grille, or a short
  duct onto the L9a. Intake where the blower sucks. Exhaust at the GPU
  bracket. Do not seal both into one pocket.

HomeCloud (no GPU) is unchanged: Pi 5, 8 GB, any SSD. See the README table.
