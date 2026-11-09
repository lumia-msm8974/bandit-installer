"""Entry point: arguments, privileges, then the web or the text front end."""
import argparse
import os
import sys

from . import devices, i18n, steps, sysdeps
from .core import UI, Abort, Cancelled, Kit, State, stamp
from .i18n import L
from .phone import Phone, Poller


class TextUI(UI):
    """Plain terminal front end (--text): for SSH or machines without a browser."""

    COLORS = {"step": "\033[1;36m", "ok": "\033[32m", "warn": "\033[33m", "error": "\033[31m", "detail": "\033[2m"}

    def emit(self, kind, text):
        c = self.COLORS.get(kind, "")
        prefix = {"step": "\n==> ", "ok": "  OK ", "warn": "  ! ", "error": "  X ", "detail": "    "}.get(kind, "    ")
        print(f"{c}{prefix}{text}\033[0m" if c else prefix + text)

    def emit_block(self, title, lines):
        print(f"\n  [{title}]")
        for l in lines:
            print("    " + l)

    def progress(self, label, done, total):
        if total:
            pct = done * 100 // total
            bar = "#" * (pct // 4)
            print(f"\r  {bar:<25} {pct:3d}%  {label[:50]:<50}", end="" if done < total else "\n", flush=True)

    def status(self, text):
        print(f"\r  {text[:90]:<90}", end="", flush=True)

    def _input(self, prompt):
        try:
            return input(prompt).strip()
        except EOFError:
            raise Cancelled()

    def confirm(self, title, body, yes, no=None, word=None, danger=False):
        print(f"\n  {title}\n  {body}")
        if word:
            return self._input(L(f"  Scrivi {word} per continuare: ", f"  Type {word} to continue: ")) == word
        return self._input(f"  {yes}? [s/N] " if i18n.get_lang() == "it" else f"  {yes}? [y/N] ").lower() in ("s", "si", "y", "yes")

    def choose(self, title, body, options):
        print(f"\n  {title}" + (f"\n  {body}" if body else ""))
        for i, o in enumerate(options, 1):
            dis = "  (-)" if o.get("disabled") else ""
            print(f"   {i}. {o['title']}{dis}" + (f" — {o['sub']}" if o.get("sub") else ""))
        while True:
            a = self._input(L("  Numero (vuoto = annulla): ", "  Number (empty = cancel): "))
            if not a.isdigit() or not 1 <= int(a) <= len(options):
                return None
            if not options[int(a) - 1].get("disabled"):
                return options[int(a) - 1]["value"]
            print(L("  Non disponibile.", "  Not available."))

    def form(self, title, body, fields, submit):
        print(f"\n  {title}" + (f"\n  {body}" if body else ""))
        out = {}
        for f in fields:
            if f.get("disabled"):
                out[f["key"]] = f.get("value")
                continue
            t = f["type"]
            if t == "toggle":
                a = self._input(f"  {f['label']} [{'S/n' if f['value'] else 's/N'}]: " if i18n.get_lang() == "it"
                                else f"  {f['label']} [{'Y/n' if f['value'] else 'y/N'}]: ").lower()
                out[f["key"]] = f["value"] if not a else a in ("s", "si", "y", "yes")
            elif t == "select":
                v = self.choose(f["label"], "", f["options"])
                out[f["key"]] = v if v is not None else f["value"]
            else:
                a = self._input(f"  {f['label']} [{f.get('value', '')}]: ")
                out[f["key"]] = a or f.get("value", "")
        return out


def device_options(current):
    out = []
    for d in devices.DEVICES.values():
        sub = ", ".join(d.models) + (" — " + L(*d.notes) if any(d.notes) else "")
        out.append({"value": d.codename, "title": d.title + ("  ✓" if d.codename == current else ""), "sub": sub})
    return out


def choose_device_text(ui, state):
    cur = steps.current_device(state)
    v = ui.choose(L("Quale telefono?", "Which phone?"),
                  L("L'installer controlla comunque il modello sul telefono (DPP) prima di scrivere.",
                    "The installer still checks the model on the phone (DPP) before writing."), device_options(cur))
    if v:
        state.data["device"] = v
        state.save()


def _exe(path):
    return os.path.abspath(os.path.expanduser(path)) if path else None


def parse_args(argv):
    ap = argparse.ArgumentParser(prog="installer", description="Android installer for the Nokia Lumia 1520 (bandit) and 930 (martini)")
    ap.add_argument("--lang", choices=("en", "it"), help="interface language (default: English, or the one chosen in the settings)")
    ap.add_argument("--device", choices=sorted(devices.DEVICES),
                    help="phone: bandit (Lumia 1520) or martini (Lumia 930); default: the last one chosen, else bandit")
    ap.add_argument("--work", help="folder for backups, logs and progress (default: work/ next to the installer)")
    ap.add_argument("--text", action="store_true", help="terminal interface instead of the app window")
    ap.add_argument("--mode", choices=sorted(steps.MODES), help="start this install mode without the menu (full, clean, update)")
    ap.add_argument("--action", choices=sorted(steps.ACTIONS), help="with --text: run one task and exit")
    ap.add_argument("--no-elevate", action="store_true", help="do not ask for administrator/root rights")
    ap.add_argument("--no-browser", action="store_true", help="only print the address of the app window")
    ap.add_argument("--rom", help="ROM zip (default: the lineage-18.1-*-<codename>.zip in rom/)")
    ap.add_argument("--rom-sha256", help="expected SHA-256 of the ROM zip (default: SHA256SUMS next to it)")
    ap.add_argument("--twrp", help="TWRP image (default: the phone's one in twrp/); its SHA-256 must be in checksums.sha256")
    ap.add_argument("--phase2-recovery", help="recovery image for phase 2 instead of the TWRP (advanced)")
    ap.add_argument("--adb", help="adb executable to use")
    ap.add_argument("--fastboot", help="fastboot executable to use")
    ap.add_argument("--serial", help="use only the phone with this adb/fastboot serial (also ANDROID_SERIAL)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--pause-at-end", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--test", action="store_true", help=argparse.SUPPRESS)
    return ap.parse_args(argv)


def main(script, argv):
    args = parse_args(argv)
    i18n.set_lang(args.lang or i18n.detect())
    root = os.path.dirname(os.path.abspath(script))
    system = sysdeps.get_system(root, test_mode=args.test)
    if sys.version_info < (3, 8):
        sys.exit("Python 3.8 or newer is needed")
    if not args.no_elevate and not system.is_admin():
        print(L("Servono i permessi di amministratore per leggere e scrivere il telefono…",
                "Administrator rights are needed to read and write the phone…"))
        try:
            system.elevate([os.path.abspath(script)] + argv)
        except sysdeps.SysError as e:
            print(e)
            print(L("Continuo senza: backup e installazione non saranno disponibili.",
                    "Going on without them: backup and install will not be available."))
    try:
        return _run(args, root, system)
    finally:
        if args.pause_at_end:
            input(L("Premi Invio per chiudere.", "Press Enter to close."))


def _run(args, root, system):
    work = os.path.abspath(args.work or os.path.join(root, "work"))
    os.makedirs(os.path.join(work, "logs"), exist_ok=True)
    logfile = os.path.join(work, "logs", f"session-{stamp()}.log")
    state = State(os.path.join(work, "state.json"))
    if not args.lang and state.data.get("lang") in ("en", "it"):
        i18n.set_lang(state.data["lang"])
    if args.device:
        state.data["device"] = args.device
        state.save()
    kit = Kit(os.path.join(root, "payload"))
    log_target = {}
    phone = Phone(system, lambda t: log_target["ui"].detail(t) if "ui" in log_target else None,
                  adb=_exe(args.adb), fastboot=_exe(args.fastboot), serial=args.serial)
    opts = {"rom": args.rom, "rom_sha256": args.rom_sha256, "twrp": args.twrp, "phase2_recovery": args.phase2_recovery}
    poller = Poller(phone, interval=3 if sysdeps.IS_WINDOWS else 1.5)

    def ctx_factory(ui):
        log_target["ui"] = ui
        return steps.Ctx(root, work, system, phone, poller, state, kit, ui, opts)

    try:
        if args.text:
            ui = TextUI(logfile)
            ctx = ctx_factory(ui)
            names = {"backup": "Backup", "firmware": "Firmware", "chain": L("Catena di avvio", "Boot chain"),
                     "verify": L("Verifica lk1st", "Check lk1st"),
                     "phase2": L("Partizioni Android (fase 2)", "Android partitions (phase 2)"),
                     "boot": L("Avvio in RAM", "Boot in RAM"),
                     "ramoops": L("Log del boot precedente", "Previous boot log"), "flash": L("Scrivi partizione", "Write partition"),
                     "restore": L("Ripristino EFIESP", "Restore EFIESP"), "diag": L("Diagnostica", "Diagnostics"),
                     "tools": L("Strumenti", "Tools")}
            once = args.mode or args.action
            if not once and not args.device:
                choose_device_text(ui, state)
                ctx = ctx_factory(ui)
            while True:
                action = once
                if not action:
                    dev = ctx.device
                    action = ui.choose(f"{dev.name} — Android", L(f"Stato: {phone.snapshot().text()}", f"Mode: {phone.snapshot().text()}"),
                                       steps.mode_options(dev) + [
                                           {"value": "advanced", "title": L("Strumenti avanzati", "Advanced tools"),
                                            "sub": L("i singoli passi, prove con lk1st, ripristino", "single steps, lk1st tests, restore")},
                                           {"value": "device", "title": L("Cambia telefono", "Change phone"),
                                            "sub": dev.title}])
                    if action == "device":
                        choose_device_text(ui, state)
                        ctx = ctx_factory(ui)
                        continue
                    if action == "advanced":
                        action = ui.choose(L("Strumenti avanzati", "Advanced tools"), "",
                                           [{"value": k, "title": v} for k, v in names.items()])
                        if not action:
                            continue
                if not action:
                    return 0
                try:
                    steps.ACTIONS[action](ctx)
                except Cancelled:
                    ui.warn(L("Annullato.", "Cancelled."))
                    if once:
                        return 1
                except (Abort, sysdeps.SysError) as e:
                    ui.error(str(e))
                    if once:
                        return 1
                if once:
                    return 0
        from . import web
        app = web.App(ctx_factory, system, phone, poller, state, kit, logfile, root)
        url = app.serve(args.port)
        poller.start()
        if args.mode:
            app.run_task(args.mode)
        web.main_loop(app, url, open_browser=not args.no_browser)
        return 0
    finally:
        for p in (work,):
            system.give_back(p)
