"""
network_check.py
-----------------
Diagnostic réseau rapide, à instancier et appeler en tout début de script
(ex: gpxcities.py) pour détecter les faux problèmes réseau (VPN, DNS
pollué, panne serveur) avant de perdre du temps à debugger le code
applicatif.

Usage dans gpxcities.py :

    from network_check import NetworkDiagnostic

    diag = NetworkDiagnostic(suspect_host="z.overpass-api.de")
    diag.run()
"""

from dataclasses import dataclass, field
import socket
import subprocess
import time
import platform


@dataclass
class NetworkDiagnosticResult:
    vpn_interfaces: list = field(default_factory=list)
    control_ok: bool = None
    control_time: float = None
    suspect_ok: bool = None
    suspect_time: float = None
    suspect_error: str = None
    verdict: str = ""
    verdict_message: str = ""


class NetworkDiagnostic:
    """Diagnostique la connectivité réseau vers un host suspect, en la
    comparant à un site de contrôle toujours disponible, et détecte la
    présence d'un VPN/tunnel actif.
    """

    VPN_INTERFACE_KEYWORDS = ("utun", "tun", "ppp", "tap", "wg")

    def __init__(self, suspect_host, control_host="www.google.com",
                 port=443, timeout=8, verbose=True):
        self.suspect_host = suspect_host
        self.control_host = control_host
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.result = NetworkDiagnosticResult()

    def run(self):
        """Exécute le diagnostic complet et retourne le résultat.

        Ne lève jamais d'exception : c'est un diagnostic, pas un blocage
        pour le reste du script.
        """
        self._log(f"\n--- Diagnostic réseau rapide (cible: {self.suspect_host}) ---")

        self.result.vpn_interfaces = self._detect_vpn_interfaces()
        if self.result.vpn_interfaces:
            self._log(f"  ⚠️  Interface(s) VPN/tunnel détectée(s): {self.result.vpn_interfaces} "
                       f"(WARP, Tailscale, etc. ? à tester en désactivant si problème réseau)")

        self._check_connectivity()
        self._build_verdict()

        self._log(f"  Verdict: {self.result.verdict} -> {self.result.verdict_message}\n")
        return self.result

    def _log(self, message):
        if self.verbose:
            print(message)

    def _detect_vpn_interfaces(self):
        system = platform.system()
        try:
            if system == "Darwin":
                out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
            elif system == "Linux":
                out = subprocess.run(["ip", "addr"], capture_output=True, text=True, timeout=5).stdout
            else:
                out = subprocess.run(["ipconfig", "/all"], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return []

        found = set()
        for line in out.splitlines():
            stripped = line.strip()
            if ":" in stripped and stripped.startswith(self.VPN_INTERFACE_KEYWORDS):
                found.add(stripped.split(":")[0])
        return sorted(found)

    def _test_connection(self, host):
        start = time.time()
        try:
            with socket.create_connection((host, self.port), timeout=self.timeout):
                return True, time.time() - start, None
        except Exception as exc:
            return False, time.time() - start, str(exc)

    def _check_connectivity(self):
        control_ok, control_time, _ = self._test_connection(self.control_host)
        suspect_ok, suspect_time, suspect_error = self._test_connection(self.suspect_host)

        self.result.control_ok = control_ok
        self.result.control_time = control_time
        self.result.suspect_ok = suspect_ok
        self.result.suspect_time = suspect_time
        self.result.suspect_error = None if suspect_ok else suspect_error

        self._log(f"  {self.control_host:25s} -> {'OK' if control_ok else 'ECHEC'} en {control_time:.2f}s")
        status = 'OK' if suspect_ok else 'ECHEC'
        detail = "" if suspect_ok else f" ({suspect_error})"
        self._log(f"  {self.suspect_host:25s} -> {status} en {suspect_time:.2f}s{detail}")

    def _build_verdict(self):
        r = self.result

        if r.control_ok and not r.suspect_ok:
            r.verdict = "PROBLEME_SPECIFIQUE_HOST"
            r.verdict_message = ("Internet général OK, mais pas ce host précis. "
                                  "Probable panne serveur, ban IP, ou VPN qui plombe ce host.")
            if r.vpn_interfaces:
                r.verdict_message += " -> Teste en désactivant ton VPN."

        elif not r.control_ok and not r.suspect_ok:
            r.verdict = "PROBLEME_RESEAU_GENERAL"
            r.verdict_message = "Rien ne passe, même le site de contrôle. Souci réseau général (Wi-Fi/FAI)."

        elif r.control_ok and r.suspect_ok and r.suspect_time > max(r.control_time * 3, 3):
            r.verdict = "LENTEUR_SUSPECTE"
            r.verdict_message = ("Les deux répondent, mais le host suspect est nettement "
                                  "plus lent que la normale.")

        else:
            r.verdict = "RESEAU_OK"
            r.verdict_message = ("Connectivité normale. Si un problème persiste, il est "
                                  "probablement applicatif (code, timeout trop court, requête malformée).")


if __name__ == "__main__":
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else "z.overpass-api.de"
    NetworkDiagnostic(host).run()
