"""The phones the installer knows, one profile each.

Everything that differs between the MSM8974 Lumias lives here: names, model codes,
ROM and TWRP files, the payload folder with lk1st and test kernels, what is ready.
The rest of the installer (BCD, phase 2 layout, provisioning, TWRP over adb) is the
same on every phone: same Windows Phone 8.1 boot manager, same Spec B partition set.

How the installer knows which phone is connected:
  - mass storage: TYPE in DPP Nokia/product.dat (for example "TYPE:RM-937");
  - TWRP or Android: ro.product.device (the codename);
  - otherwise the profile chosen at start (--device, or the one saved for that phone).
A phone that reports another model than the chosen profile stops the task.
"""
import re


class Device:
    def __init__(self, codename, name, models, ready, rom_releases, twrp_name=None, twrp_page=None,
                 twrp_url=None, unverified_models=(), notes=("", "")):
        self.codename = codename
        self.name = name                    # "Lumia 1520"
        self.models = tuple(models)         # RM codes confirmed for this codename
        self.unverified_models = tuple(unverified_models)
        self.ready = dict(ready)            # mode -> bool
        self.rom_glob = f"lineage-18.1-*-{codename}.zip"
        self.rom_devices = (codename,) + self.models + self.unverified_models
        self.rom_releases = rom_releases
        self.twrp_name = twrp_name
        self.twrp_page = twrp_page
        self.twrp_url = twrp_url
        self.payload = codename             # folder under payload/
        self.notes = notes                  # (it, en) shown next to the device choice

    @property
    def title(self):
        return f"{self.name} ({self.codename})"

    def all_models(self):
        return self.models + self.unverified_models

    def matches_model(self, model):
        return normalize_model(model) in self.all_models()

    def twrp_file(self, name):
        """True when a file name looks like a TWRP image for this phone (twrp-…-<codename>.img)."""
        n = name.lower()
        return n.startswith("twrp") and n.endswith(".img") and re.search(rf"[-_.]{self.codename}[-_.]", n) is not None


BANDIT = Device(
    "bandit", "Lumia 1520", ("RM-937", "RM-938", "RM-939", "RM-940"),
    ready={"full": True, "clean": True, "update": True},
    rom_releases="https://github.com/lumia-msm8974/android_device_nokia_bandit/releases",
    twrp_name="twrp-3.7.0_9-1-bandit.img",
    twrp_page="https://github.com/lumia-msm8974/android_device_nokia_bandit/releases/tag/twrp-bandit-20261102",
    twrp_url=("https://github.com/lumia-msm8974/android_device_nokia_bandit/releases/download/"
              "twrp-bandit-20261102/twrp-3.7.0_9-1-bandit.img"),
)

# Lumia 930: RM-1045. RM-1087 is listed as a 930 by one source only. The Lumia Icon
# (929, RM-927) shares the codename but has its own Windows firmware set (VANQUISH),
# so it is not accepted.
MARTINI = Device(
    "martini", "Lumia 930", ("RM-1045",), unverified_models=("RM-1087",),
    ready={"full": False, "clean": False, "update": False},
    rom_releases="https://github.com/lumia-msm8974/android_device_nokia_martini/releases",
    # Built from android_device_nokia_martini branch twrp-9.0 (ad8b259), never booted, not released: the
    # file goes in twrp/ by hand. Pinned in checksums.sha256 so phase 2 (advanced tools) can use it.
    twrp_name="twrp-3.7.0_9-0-martini.img",
    notes=("In preparazione: lk1st, TWRP e ROM compilati ma mai avviati; le modalità si sbloccano dopo la prima prova sul telefono "
           "(fase 2 disponibile negli strumenti avanzati).",
           "In preparation: lk1st, TWRP and ROM built but never booted; the modes unlock after the first test on a phone "
           "(phase 2 available in the advanced tools)."),
)

DEVICES = {d.codename: d for d in (BANDIT, MARTINI)}
DEFAULT = BANDIT.codename


def normalize_model(model):
    """'rm-1045', 'RM1045', ' RM-1045 ' -> 'RM-1045'."""
    m = re.fullmatch(r"\s*RM-?(\d{3,4})\s*", model or "", re.I)
    return f"RM-{m.group(1)}" if m else (model or "").strip().upper()


def by_model(model):
    """The profile for an RM code, or None (also for the Lumia Icon RM-927)."""
    for d in DEVICES.values():
        if d.matches_model(model):
            return d
    return None


def parse_product_dat(text):
    """{'TYPE': 'RM-937', …} from DPP Nokia/product.dat (KEY:VALUE lines)."""
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().upper()] = v.strip()
    return out


def get(codename):
    return DEVICES[codename]
