Put the ROM here, with its SHA256SUMS (from the same download page):

  Lumia 1520: lineage-18.1-YYYYMMDD-UNOFFICIAL-bandit.zip
              https://github.com/lumia-msm8974/android_device_nokia_bandit/releases
  Lumia 930:  lineage-18.1-YYYYMMDD-UNOFFICIAL-martini.zip
              https://github.com/lumia-msm8974/android_device_nokia_martini/releases

The installer checks the zip against SHA256SUMS before it touches the phone, and only uses a zip
made for the phone you picked. Keep one zip here (with more than one the installer asks which),
or pass --rom <path>.

Metti qui la ROM (lo zip lineage-18.1-...-UNOFFICIAL-bandit.zip per il 1520, -martini.zip per il
930) e il file SHA256SUMS della stessa pagina di download. L'installer controlla lo zip prima di
toccare il telefono.
