Put the TWRP image here:

  Lumia 1520: twrp-3.7.0_9-1-bandit.img
              https://github.com/lumia-msm8974/android_device_nokia_bandit/releases/tag/twrp-bandit-20261102
  Lumia 930:  twrp-3.7.0_9-0-martini.img
              https://github.com/lumia-msm8974/android_device_nokia_martini/releases/tag/twrp-martini-20261113

The installer offers to download it when it is missing. Its SHA-256 must be listed in
checksums.sha256 next to the installer. TWRP is only started in RAM (fastboot boot), never
written to the phone.

Metti qui la TWRP del tuo telefono (l'installer la scarica da solo se manca). Lo SHA-256 deve
essere in checksums.sha256. La TWRP viene solo avviata in RAM, mai scritta sul telefono.
