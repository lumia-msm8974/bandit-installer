# Installer Android per Lumia 1520: guida

[English](GUIDE.md) · [README](../README.md)

Questa guida porta un Lumia 1520 con Windows 10 Mobile (o Windows Phone 8.1) al port non ufficiale
di **LineageOS 18.1** (Android 11), e spiega come reinstallarlo o aggiornarlo in seguito.

> **Lumia 930 (martini, RM-1045):** l'installer lo conosce già, ma per ora sono disponibili solo backup,
> estrazione del firmware e catena di avvio, con un lk1st mai avviato. Per il 930 non ci sono ancora ROM
> né TWRP, quindi Completa, Pulita e Aggiornamento restano bloccate. Scegli il telefono nella pagina di
> installazione (o con `--device martini`); l'installer controlla il modello sul telefono prima di
> scrivere qualsiasi cosa. Il resto della guida descrive il Lumia 1520.

> **Non ufficiale e sperimentale.** L'installazione **Completa** **cancella Windows 10 Mobile e tutto
> quello che c'è sul telefono** (app, foto, file). Per tornare a Windows serve un'immagine FFU di
> Windows scritta con WPinternals o Windows Device Recovery Tool. Fai i backup che l'installer propone
> e conservali.

## Prima di cominciare: sbloccare il bootloader (solo Windows)

**WPinternals è uno strumento separato**, di René Lergner
([github.com/ReneLergner/WPinternals](https://github.com/ReneLergner/WPinternals)). **Non fa parte di
questo installer** e **funziona solo su Windows** (Windows 10 o 11). Serve solo per l'installazione
**Completa**, e prima che l'installer tocchi il telefono:

1. **Sblocca il bootloader** con WPinternals su un PC Windows. È sempre il primo passo, fatto su
   Windows, anche se poi userai l'installer su Linux. Senza sblocco BootShim non parte e l'installer si
   ferma («manca IS_UNLOCKED»).
2. **Passa in mass storage** con WPinternals (*Switch to Mass Storage Mode*). La modalità mass storage
   si attiva solo da WPinternals, quindi solo da Windows. Il telefono mostra un simbolo USB e le sue
   partizioni compaiono sul PC.

| Usi l'installer su | Procedura |
|---|---|
| Windows | WPinternals: sblocco, poi mass storage; poi avvia l'installer sullo stesso PC. |
| Linux | Sul PC Windows, WPinternals: sblocco, poi mass storage. Sposta il cavo USB sul PC Linux (il telefono resta in mass storage fino al riavvio) e avvia lì l'installer. |

Se il telefono si riavvia prima che la catena di avvio sia installata (per esempio hai interrotto il
backup e lo hai riavviato), esce dal mass storage: rimettilo in mass storage da WPinternals su Windows e
sposta di nuovo il cavo. Dalla verifica di lk1st in poi l'installer usa solo fastboot e adb, su Windows o
Linux. **Pulita** e **Aggiornamento** non hanno mai bisogno di WPinternals. WPinternals serve di nuovo
solo per tornare a Windows (scrittura di un FFU dalla modalità flash,
[sezione 13](#13-tornare-a-windows)).

## Indice

1. [Quale modalità?](#1-quale-modalità)
2. [Cosa serve](#2-cosa-serve)
3. [Download](#3-download)
4. [Preparare il PC](#4-preparare-il-pc)
5. [Avviare l'installer](#5-avviare-linstaller)
6. [Installazione completa, passo per passo](#6-installazione-completa-passo-per-passo)
7. [Installazione pulita](#7-installazione-pulita)
8. [Aggiornamento](#8-aggiornamento)
9. [Portare il telefono in lk1st (fastboot)](#9-portare-il-telefono-in-lk1st-fastboot)
10. [Dopo l'installazione](#10-dopo-linstallazione)
11. [Limiti noti](#11-limiti-noti)
12. [Problemi comuni e riavvii continui](#12-problemi-comuni-e-riavvii-continui)
13. [Tornare a Windows](#13-tornare-a-windows)
14. [Strumenti avanzati](#14-strumenti-avanzati)
15. [File e opzioni](#15-file-e-opzioni)

## 1. Quale modalità?

| Il telefono adesso | Modalità | I tuoi dati |
|---|---|---|
| Windows 10 Mobile o 8.1, mai avuto Android | **Completa** | Windows e tutto il contenuto del telefono cancellati |
| Ha già questo Android, vuoi ripartire da zero (o non si avvia più) | **Pulita** | tutti i dati Android cancellati, memoria interna compresa |
| Ha già questo Android, vuoi una build più recente | **Aggiornamento** | conservati |

## 2. Cosa serve

- Un **Nokia Lumia 1520** (RM-937, RM-938, RM-939 o RM-940).
- Per la **Completa**: un **PC Windows 10/11** per lo sblocco con
  [WPinternals](https://github.com/ReneLergner/WPinternals). L'installer funziona su quel PC oppure su
  un PC **Linux**.
- Un buon cavo USB, collegato direttamente al PC (niente hub). Uno difettoso si nota come «due letture
  diverse» o come telefono che sparisce da adb.
- La batteria **sopra il 50%**: in TWRP la porta USB del PC la carica poco.
- Spazio libero sul PC: circa 1 GB per il backup di base, più la copia compressa dell'intero telefono
  se tieni l'immagine completa (fortemente consigliata prima della Completa: da qualche GB a decine di
  GB), più 600 MB per la ROM.
- Tempo: Completa 45-120 minuti (quasi tutti per il backup completo), Pulita e Aggiornamento 10-15
  minuti.

## 3. Download

| Cosa | Da dove | Dove metterlo |
|---|---|---|
| L'installer: `bandit-installer-<versione>-windows.zip` (con il suo Python) oppure `bandit-installer-<versione>-linux.tar.gz` | [release dell'installer](https://github.com/lumia-msm8974/bandit-installer/releases) | una cartella con abbastanza spazio libero |
| La ROM: `lineage-18.1-AAAAMMGG-UNOFFICIAL-bandit.zip` **e** `SHA256SUMS` | [release di android_device_nokia_bandit](https://github.com/lumia-msm8974/android_device_nokia_bandit/releases) | la cartella `rom` dell'installer |
| La TWRP: `twrp-3.7.0_9-1-bandit.img` (facoltativa: se manca la scarica l'installer) | [release `twrp-bandit-20261102`](https://github.com/lumia-msm8974/android_device_nokia_bandit/releases/tag/twrp-bandit-20261102) | la cartella `twrp` dell'installer |

Su Windows estrai prima tutto lo zip dell'installer (tasto destro → *Estrai tutto*); non avviarlo
dall'interno dello zip. Tieni **un solo** zip della ROM in `rom` (se ce ne sono di più l'installer
chiede quale).

L'installer controlla lo zip della ROM con `SHA256SUMS` e l'immagine della TWRP con il proprio
`checksums.sha256` **prima di toccare il telefono**: un download danneggiato lo ferma con un messaggio
chiaro.

## 4. Preparare il PC

### Windows

1. Installa il [driver USB di Google](https://developer.android.com/studio/run/win-usb), perché Windows
   riconosca il telefono in fastboot e in adb. In Gestione dispositivi il telefono deve comparire come
   *Android Bootloader Interface* in fastboot e *Android ADB Interface* in TWRP; se ha un triangolo
   giallo: tasto destro → *Aggiorna driver* → *Cerca nel computer* → la cartella del driver di Google.
2. Serve **Edge** o **Chrome** (Edge fa parte di Windows): l'installer si apre in una finestra app.
3. `fastboot` e `adb` non servono in anticipo: l'installer propone di scaricare i Platform Tools di
   Google nella sua cartella `tools\`.

### Linux

Installa Python 3 (3.8 o successivo), il modulo Python di hivex (per modificare il BCD), util-linux e
il supporto NTFS (per il passo del firmware):

```bash
sudo pacman -S python hivex util-linux ntfs-3g android-tools              # Arch
sudo apt install python3 python3-hivex util-linux ntfs-3g adb fastboot      # Debian, Ubuntu
sudo dnf install python3 python3-hivex util-linux ntfs-3g android-tools   # Fedora
```

Per la finestra app serve Chromium, Chrome, Brave o Edge; altrimenti si apre una scheda del browser
predefinito, e `--text` dà un'interfaccia nel terminale. Se mancano `adb`/`fastboot` l'installer può
scaricare la copia di Google.

## 5. Avviare l'installer

| Sistema | Come |
|---|---|
| Windows | doppio clic su `installer.bat` e accetta la richiesta di amministratore |
| Linux | `./installer.sh` in un terminale; chiede la password con `sudo` |

I permessi di amministratore (root) servono per leggere e scrivere il disco del telefono in mass
storage (Completa). Pulita e Aggiornamento usano solo adb e fastboot: per loro basta `--no-elevate`.

La finestra mostra:

- a sinistra, lo **stato del telefono** (non collegato, mass storage, fastboot, recovery…); un clic
  avvia la diagnostica;
- **Installa**, con le tre modalità, l'avanzamento dell'installazione completa e i requisiti (adb,
  fastboot, TWRP, ROM);
- **Strumenti avanzati**, con ogni passo da solo;
- **Impostazioni**, per lingua (inglese o italiano), colore d'accento e cartella dati.

La finestra comunica con l'installer tramite un indirizzo locale (`127.0.0.1`) mostrato nel terminale.
Se non si apre nessuna finestra, copia quell'indirizzo in un browser. Chiudere la finestra non ferma
l'installer: usa **Impostazioni → Esci**, oppure Ctrl+C nel terminale. Nell'interfaccia da terminale
(`--text`) l'installer fa le stesse domande come testo.

Quando l'installer chiede di **scrivere una parola** (`INSTALLA`, `CANCELLA WINDOWS`, `CANCELLA`,
`AGGIORNA`; in inglese `INSTALL`, `ERASE WINDOWS`, `ERASE`, `UPDATE`), sta per scrivere sul telefono.
Qualunque altra risposta lo ferma senza scrivere niente.

## 6. Installazione completa, passo per passo

**Prima di cominciare:** Windows e tutto il contenuto del telefono verranno cancellati. Copia prima
foto e file dal telefono.

### 6.1 Sbloccare il telefono e metterlo in mass storage

Questa parte si fa con **WPinternals su Windows**, fuori da questo installer (vedi
[Prima di cominciare](#prima-di-cominciare-sbloccare-il-bootloader-solo-windows)).

1. Segui le istruzioni di WPinternals per **sbloccare il bootloader**.
2. In WPinternals scegli **Switch to Mass Storage Mode**. Il telefono mostra un simbolo USB e le sue
   partizioni compaiono sul PC.
3. Installer su Linux: ora sposta il cavo USB sul PC Linux. Il telefono resta in mass storage fino al
   riavvio.

### 6.2 Avvio

Metti lo zip della ROM e `SHA256SUMS` in `rom`, avvia l'installer e premi **Inizia** su **Installazione
completa** (oppure `--mode full`). L'installer:

1. propone i Platform Tools se mancano `adb` o `fastboot`;
2. controlla l'immagine della TWRP (se manca propone di scaricarla) e lo zip della ROM.

### 6.3 Backup

Trova il disco del telefono dalla tabella delle partizioni e chiede se fare anche l'**immagine completa
compressa**. **Rispondi sì**: è l'unica copia esatta di Windows. Poi salva la tabella delle partizioni,
ogni partizione sotto i 512 MiB (letta **due volte** e confrontata), l'immagine completa e un elenco
`SHA256SUMS`. Con l'immagine completa servono 20-60 minuti.

Il backup contiene dati personali e del modem (DPP, MODEM_FS*): conservalo e non pubblicarlo mai.

### 6.4 Firmware da Windows

Copia dalle partizioni di Windows il firmware, le calibrazioni audio e Wi-Fi, le tabelle ACPI e il
registro, come riferimento e per lo sviluppo. Sono file Nokia/Microsoft: restano sul tuo PC. (La ROM
porta il proprio firmware.)

### 6.5 Catena di avvio

1. Mostra la configurazione di avvio attuale (BCD) e le **opzioni di avvio**: nella Completa Android è
   il sistema predefinito e l'unico, perché Windows sta per essere cancellato. Puoi impostare
   un'**attesa del menu** (0 = nessun menu).
2. Fa il backup di EFIESP (file e immagine grezza), scrive il nuovo BCD accanto al backup, lo rilegge e
   lo verifica.
3. Scrivi **INSTALLA**; scrive BootShim, lk1st e il BCD su EFIESP e confronta ogni file.

**Il volume su all'accensione apre sempre la modalità flash**: è la via del ritorno con WPinternals, e
l'installer rifiuta qualunque configurazione che la cambierebbe.

### 6.6 Riavvio e verifica di lk1st

Riavvia il telefono: esci dalla modalità mass storage in WPinternals, oppure tieni premuti
**accensione + volume giù** per circa 10 secondi finché vibra. Compare il testo di BootShim, poi lo
schermo resta fermo: lk1st non usa il display, quindi è normale. L'installer aspetta fastboot e salva
`fastboot getvar all` e il log di lk1st.

### 6.7 Partizioni Android (fase 2)

1. La TWRP viene avviata **in RAM** con `fastboot boot` (non viene scritta sul telefono). Aspetta la
   schermata della TWRP; l'installer aspetta adb.
2. Controlla che adb sia root, che ci siano gli strumenti necessari e che niente della memoria del
   telefono sia montato.
3. Calcola la nuova tabella delle partizioni e la mostra: MMOS, MainOS e Data spariscono; al loro posto,
   subito dopo EFIESP, aboot, boot, recovery, misc, modem, cache, persist, persistent, fsc, fsg,
   metadata, ssd, modemst1, modemst2, vendor, system e userdata. Tutte le altre partizioni restano come
   sono.
4. Controlla che lk1st sia su EFIESP e sia la voce di avvio predefinita.
5. Copia le **11 partizioni di provisioning** (DPP, MODEM_FS1, MODEM_FS2, MODEM_FSC, MODEM_FSG, SSD, DDR,
   DBI, UEFI_BS_NV, UEFI_RT_NV, BACKUP_BS_NV) in `work/backups/phase2-<data>/provisioned/` e le
   confronta con il telefono.
6. Mostra un riepilogo e chiede di scrivere **CANCELLA WINDOWS**. Da qui non si torna indietro.
7. Scrive la nuova tabella (prima la copia di riserva in fondo al disco, poi la primaria), la rilegge e
   la verifica con un lettore indipendente.
8. Formatta le nuove partizioni, copia lk1st in `aboot` (resta anche su EFIESP), copia i dati del modem
   in modemst1/modemst2/fsc/fsg/ssd e scrive in `persist` gli indirizzi MAC Wi-Fi e Bluetooth e la
   calibrazione Wi-Fi presi dal DPP.

Se il kernel non vede subito le nuove partizioni, l'installer riavvia da solo il telefono in lk1st e
nella TWRP.

### 6.8 ROM

1. La TWRP viene riavviata (così vede le nuove partizioni).
2. `format data`, `wipe cache`, `wipe dalvik`.
3. **adb sideload** della ROM. Il trasferimento si ferma intorno al **47%** con `Total xfer: 1.00x`: è
   normale. La TWRP installa mentre riceve, qualche minuto.
4. L'installer legge il log della TWRP (deve dire `script succeeded: result was [1.000000]`), controlla
   che la partizione boot corrisponda a `boot.img` dello zip, salva il log in `work/logs/rom-<data>/` e
   riavvia il telefono.

Il **primo avvio richiede diversi minuti**: l'animazione di LineageOS resta a lungo sullo schermo.
Lascia stare il telefono finché non compare la configurazione iniziale.

### 6.9 Se si ferma

Risolvi la causa mostrata in rosso e avvia di nuovo la **Completa**: riprende da dove si era fermata. I
passi già fatti vengono proposti come *Salta*. Se la nuova tabella era già scritta quando si è fermata,
la volta dopo formatta e rifà il provisioning senza chiedere una seconda volta di cancellare Windows.

## 7. Installazione pulita

Per un telefono che ha già lk1st e le partizioni Android (per esempio dopo una Completa). **Tutti i
dati Android vengono cancellati**, memoria interna compresa.

1. Metti lo zip della ROM e `SHA256SUMS` in `rom`.
2. Collega il telefono: in Android con il **debug USB** attivo (Impostazioni → Sistema → Opzioni
   sviluppatore; autorizza il PC quando il telefono lo chiede), oppure già in lk1st fastboot
   ([sezione 9](#9-portare-il-telefono-in-lk1st-fastboot)).
3. Avvia **Installazione pulita** (oppure `--mode clean`). L'installer controlla TWRP e ROM, riavvia il
   telefono in lk1st, avvia la TWRP in RAM e controlla che ci siano le partizioni Android.
4. Scrivi **CANCELLA**. La TWRP formatta i dati e pulisce cache e Dalvik.
5. La ROM viene installata con sideload e verificata, il telefono si riavvia. Il primo avvio richiede
   diversi minuti.

## 8. Aggiornamento

Per un telefono che ha già questo port di LineageOS 18.1. **I dati restano.**

1. Metti il nuovo zip della ROM e il suo `SHA256SUMS` in `rom` (togli lo zip vecchio).
2. In Android attiva il **debug USB** (Impostazioni → Sistema → Opzioni sviluppatore), collega il
   telefono e autorizza il PC quando lo chiede.
3. Avvia **Aggiornamento** (oppure `--mode update`). L'installer controlla TWRP e ROM, riavvia il
   telefono in lk1st, avvia la TWRP in RAM e legge il sistema installato. Se non è LineageOS 18.1 per
   bandit ti avvisa: aggiornare un altro sistema senza formattare di solito finisce in un riavvio
   continuo, quindi scrivi **AGGIORNA** solo se sai perché; altrimenti usa la Pulita.
4. La ROM viene installata con sideload e verificata, il telefono si riavvia.

## 9. Portare il telefono in lk1st (fastboot)

L'installer ha bisogno di lk1st in fastboot per avviare la TWRP. Quasi sempre ci arriva da solo:

| Il telefono è… | Cosa fare |
|---|---|
| in Android, debug USB attivo | niente: l'installer esegue `adb reboot bootloader` (circa 12 s) |
| nella TWRP | niente: l'installer lo riavvia in lk1st; a mano: *Riavvia → Bootloader* |
| acceso senza Android installato (dopo la catena di avvio) | accendilo: dopo il testo di BootShim lk1st resta in fastboot con lo schermo fermo |
| nel menu di avvio di Windows (se hai impostato un'attesa) | scegli **Android (lk1st)** |
| in un riavvio continuo | vedi la [sezione 12](#12-problemi-comuni-e-riavvii-continui) |

**Il volume su tenuto all'accensione non è fastboot**: apre la **modalità flash** di Windows Phone (un
grande fulmine), usata da WPinternals e da Windows Device Recovery Tool. È la via del ritorno quando
nient'altro funziona; l'installer non la cambia mai.

## 10. Dopo l'installazione

| Fai | Il telefono avvia |
|---|---|
| accensione | Android, attraverso UEFI → bootmgr → BootShim → lk1st |
| accensione con volume su | modalità flash, per WPinternals |
| `adb reboot bootloader` | lk1st in fastboot |

Per **aggiornare in seguito** vedi la [sezione 8](#8-aggiornamento). Conserva `work/` (backup, copia
del provisioning, log) in un posto sicuro.

## 11. Limiti noti

- `adb sideload` si ferma intorno al **47%** con `Total xfer: 1.00x`: è normale per questa ROM a blocchi.
- Lo strumento a riga di comando `twrp` scrive **`Segmentation fault`** dopo ogni comando: il comando è
  stato eseguito; l'installer legge il risultato dal log della TWRP.
- **Non aprire il telefono in un file manager o in un programma MTP** sul PC mentre l'installer lavora:
  un client MTP fa perdere il telefono ad adb nella TWRP. Copia i file con `adb push`/`adb pull`, o
  quando Android è avviato.
- La TWRP viene solo avviata in RAM; non viene installata sul telefono.
- Il primo avvio richiede diversi minuti, di più dopo Pulita o Completa.
- La fase 2 con la TWRP è stata provata solo su un telefono simulato; la stessa sequenza è stata
  eseguita su un RM-937 reale con la recovery della ROM. Se alla TWRP manca uno strumento l'installer si
  ferma prima di scrivere; l'opzione avanzata `--phase2-recovery <recovery.img>` usa allora un'altra
  recovery (serve adb come root).
- Le parti dell'installer specifiche di Windows (BCD con `bcdedit`, rilevamento di dischi e USB) non
  sono ancora state eseguite su Windows.

## 12. Problemi comuni e riavvii continui

| Problema | Cosa fare |
|---|---|
| «Lo zip della ROM non corrisponde al suo checksum» | Scarica di nuovo lo zip e `SHA256SUMS`, dalla stessa release. |
| «non è il file atteso» (TWRP) | Cancella l'immagine in `twrp` e lascia che l'installer la riscarichi. |
| «Nessun SHA-256» | Metti accanto allo zip il `SHA256SUMS` della pagina della release della ROM. |
| «Più di un dispositivo» | Scollega gli altri telefoni (anche adb via Wi-Fi), oppure usa `--serial <seriale>`. |
| fastboot o adb non compaiono (Windows) | Controlla il driver USB di Google in Gestione dispositivi. |
| La TWRP è sul telefono ma l'installer continua ad aspettare | Chiudi programmi MTP o finestre del file manager sul PC; scollega e ricollega il cavo. |
| «La TWRP segnala che l'installazione della ROM non è riuscita» | Guarda `work/logs/rom-<data>/recovery.log`; riavvia l'installer. |
| Il telefono finisce in **Qualcomm 9008** | Tieni premuti accensione + volume giù per 10 s, poi volume su all'accensione (modalità flash) e chiedi aiuto con i log. |
| **Due letture diverse** durante il backup | Cavo o porta instabili: cambiali e rifai il backup. |
| **Partizioni ancora montate** (mass storage) | Chiudi le finestre di Esplora risorse/file manager che mostrano le unità del telefono. |
| Linux: manca **hivex** | Installa il pacchetto della [sezione 4](#linux). |

### Riavvio continuo

1. Se `adb devices` mostra il telefono mentre si riavvia (debug USB attivo e questo PC autorizzato),
   avvia **Aggiornamento** (conserva i dati) o **Pulita**: l'installer interrompe il ciclo con
   `adb reboot bootloader`.
2. Se adb non compare mai: metti il telefono in modalità flash (volume su all'accensione), passalo in
   mass storage con WPinternals e, su Linux, rovina l'intestazione dell'immagine di boot così che lk1st
   resti in fastboot (non ancora provato sul telefono):
   ```bash
   lsblk -o NAME,PARTLABEL          # trova la partizione con etichetta "boot", per esempio sdb27
   sudo dd if=/dev/zero of=/dev/sdb27 bs=4096 count=1 conv=fsync   # controlla il nome due volte!
   ```
   Riavvia il telefono (accensione + volume giù per 10 s): lk1st non trova un'immagine di boot valida e
   resta in fastboot. Avvia **Aggiornamento** o **Pulita**.
3. Ultima risorsa: [torna a Windows](#13-tornare-a-windows) e ricomincia con la Completa.

I log di ogni sessione sono in `work/logs/`: allegali a ogni segnalazione, dopo aver controllato che non
contengano dati che vuoi tenere privati.

## 13. Tornare a Windows

Dopo una Completa Windows non c'è più: serve un **FFU di Windows 10 Mobile o 8.1** per il tuo modello.

1. Accendi il telefono tenendo premuto **volume su**: modalità flash.
2. Scrivi l'FFU con **WPinternals** (la sua funzione di flash dell'FFU) o con **Windows Device Recovery
   Tool**.

Prima della Completa (solo catena di avvio installata) basta **Ripristino EFIESP** negli strumenti
avanzati: rimette il BCD e i file originali.

## 14. Strumenti avanzati

| Strumento | Stato del telefono | Scrive sul telefono |
|---|---|---|
| Backup, Firmware | mass storage | no |
| Catena di avvio | mass storage | solo EFIESP, dopo un backup |
| Verifica lk1st | riavvio → fastboot | no |
| Partizioni Android | fastboot → TWRP | sì: solo la fase 2 (cancella Windows), niente ROM |
| Avvio in RAM | fastboot | no: `fastboot boot` di K1 o di un `boot.img` |
| Log del boot precedente | fastboot | no: ram console e log di lk1st dopo un blocco |
| Scrivi partizione | fastboot | sì, solo partizioni Android (`boot`, `recovery`, `system`…) |
| Ripristino EFIESP | mass storage | sì, da un backup |
| Diagnostica | qualunque | no |
| Platform Tools | — | no |

## 15. File e opzioni

| Cartella | Contenuto |
|---|---|
| `rom/` | lo zip della ROM e il suo `SHA256SUMS` |
| `twrp/` | l'immagine della TWRP |
| `work/backups/` | backup del telefono, di EFIESP e della fase 2 (partizioni di provisioning, GPT prima e dopo) |
| `work/private/` | firmware estratto da Windows |
| `work/logs/` | log delle sessioni, log di lk1st, `recovery.log` della TWRP di ogni installazione della ROM |
| `work/state.json` | avanzamento, lingua e colore |

Backup e firmware contengono dati personali e file Nokia/Microsoft: **non pubblicarli**.

| Opzione | Significato |
|---|---|
| `--mode full\|clean\|update` | avvia quella modalità senza il menu |
| `--rom <zip>`, `--rom-sha256 <hash>` | zip della ROM e il suo SHA-256 atteso (predefiniti: `rom/` e `SHA256SUMS`) |
| `--twrp <img>` | immagine della TWRP; deve essere in `checksums.sha256` |
| `--phase2-recovery <img>` | avanzata: un'altra recovery per la fase 2 |
| `--serial <seriale>` | solo questo telefono (anche `ANDROID_SERIAL`) |
| `--adb <percorso>`, `--fastboot <percorso>` | usa questi eseguibili |
| `--lang en\|it` | lingua dell'interfaccia (predefinita inglese, o l'ultima scelta) |
| `--text` | interfaccia nel terminale, per SSH o macchine senza browser |
| `--text --action <passo>` | esegue un passo ed esce (`backup`, `chain`, `verify`, `phase2`, `boot`, `diag`…) |
| `--work <cartella>` | cartella dei dati (predefinita `work/` accanto all'installer) |
| `--no-browser` | stampa solo l'indirizzo della finestra |
| `--no-elevate` | non chiede i permessi di amministratore (bastano per Pulita e Aggiornamento) |

Esempi: `installer.bat --mode update --lang it`, `./installer.sh --text --mode clean --lang it`.
