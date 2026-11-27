"""Execute the complete guard with private state and stubbed network commands.

No sudo, live route mutation, daemon signals or production state is accessible.
Run: python3 -m unittest discover -s scripts/tests -p test_zerotier_route_guard.py -v
"""

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SOURCE = Path(os.environ.get("GUARD_SOURCE", Path(__file__).parents[1] / "ops/zerotier-route-guard"))

STUB = r'''#!/usr/bin/env python3
import json, os, signal, subprocess, sys, time
from pathlib import Path
root=Path(__file__).resolve().parent
name=Path(sys.argv[0]).name
args=sys.argv[1:]
p=root/'fixture.json'
d=json.loads(p.read_text())
def save(): p.write_text(json.dumps(d))
def write():
    with (root/'writes.jsonl').open('a') as f: f.write(json.dumps([name,args])+"\n")
def hanging():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child=subprocess.Popen(['/bin/sleep','60'])
    (root/'hanging-child.pid').write_text(str(child.pid))
    while True: time.sleep(1)
if d.get('hang') == name: hanging()
if d.get('fail') == name: sys.exit(1)
if name in ('chown','logger'): sys.exit(0)
if name == 'networksetup':
    print('(Hardware Port: Wi-Fi, Device: en1)')
    if d.get('two_physical'): print('(Hardware Port: Ethernet, Device: en0)')
    if d.get('absent_service'): print('(Hardware Port: USB LAN, Device: en6)')
elif name == 'ifconfig':
    if args == ['-l']: print('en0 en1 lo0 feth1924')
    elif args == ['en6']: sys.exit(1)
    else: print('inet6 240e::1\nstatus: active')
elif name == 'ipconfig': print(d.get('address','192.168.1.7') if args[0]=='getifaddr' else '192.168.1.1')
elif name == 'netstat':
    print('default 192.168.1.1 UGSc en1')
    for key,dst in [('low','0/1'),('high','128.0/1')]:
        if not d.get(key+'_missing'):
            print(dst,d.get(key+'_gateway','192.168.1.1'),'UGSc',d.get(key+'_iface','en1'))
    if d.get('duplicate'): print('0/1 192.168.1.1 UGSc en1')
    if d.get('vpn'): print('default 10.2.3.1 UGSc utun6')
    print('10.170.51/24 link#17 UC feth1924')
elif name == 'route':
    if 'change' in args or 'add' in args:
        write()
        if d.get('mutation_fail'): sys.exit(3)
        key='low' if '0.0.0.0/1' in args else 'high'
        assert '-net' in args and '-ifa' in args, args
        d[key+'_ifa']=args[args.index('-ifa')+1]
        d[key+'_gateway']='192.168.1.1';d[key+'_iface']='en1'
        d[key+'_missing']=False
        save();sys.exit(0)
    if '-inet6' in args:
        if d.get('ipv6_missing'): sys.exit(1)
        print('interface: en1');sys.exit(0)
    if '-ifscope' in args:
        print('gateway: 192.168.1.1\ninterface:',args[args.index('-ifscope')+1]);sys.exit(0)
    dst=args[-1]
    if dst=='10.170.51.1': print('destination: 10.170.51.0\nmask: 255.255.255.0\ninterface:',d.get('zt_iface','feth1924'));sys.exit(0)
    key='low' if dst in ('1.1.1.1','0.0.0.0/1') else 'high'
    prefix=key if '-v' in args else key+'_sentinel'
    print('gateway:',d.get(prefix+'_gateway',d.get(key+'_gateway','192.168.1.1')))
    print('interface:',d.get(prefix+'_iface',d.get(key+'_iface','en1')))
    if '-v' in args:
        if not d.get('unknown_ifa'):
            print('sockaddrs: <DST,GATEWAY,NETMASK,IFP,IFA>')
            print(' default 192.168.1.1 128.0.0.0 index: 12 en1:aa.bb',d.get(key+'_ifa','192.168.1.7'))
elif name == 'scutil':
    if args==['--dns']:
        if not d.get('dns_bad'): print('DNS configuration\nresolver #1\n nameserver[0] : 192.168.1.1')
    else:
        content=sys.stdin.read()
        if 'd.remove' in content:
            write();d['dns_bad']=False;save()
        else:
            print('InterfaceName : feth1924')
            if not d.get('zt_clean'): print('Router : 127.0.0.1\nServerAddress : 127.0.0.1')
elif name == 'dscacheutil': print('ip_address: 192.0.2.8')
elif name == 'curl': print('204' if 'https://www.gstatic.com/generate_204' in args else '200',end='')
elif name == 'ndp': print('fe80::1%en1 aa:bb:cc:dd:ee:ff en1 23h S R')
elif name == 'killall': raise AssertionError('unexpected live-style action '+name)
else: raise AssertionError(name)
'''


class GuardFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="zt-guard-fixture-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        stub = self.root / "stub"
        stub.write_text(STUB)
        stub.chmod(0o755)
        script = SOURCE.read_text()
        for command in ["/usr/sbin/chown", "/usr/bin/logger", "/sbin/route", "/usr/sbin/scutil", "/usr/sbin/netstat", "/usr/sbin/networksetup", "/sbin/ifconfig", "/usr/sbin/ipconfig", "/usr/sbin/ndp", "/usr/bin/dscacheutil", "/usr/bin/curl", "/usr/bin/killall"]:
            target = self.root / Path(command).name
            target.symlink_to(stub)
            script = script.replace(command, str(target))
        script = script.replace("/etc/zerotier-route-guard.conf", str(self.root / "config"))
        script = script.replace("/var/db/zerotier-route-guard", str(self.root / "state"))
        script = script.replace("/var/run/zerotier-route-guard.lock", str(self.root / "lock"))
        script = script.replace("/Library/Application Support/ZeroTier/One/networks.d/$TARGET_SERVICE_ID.local.conf", str(self.root / "zt.conf"))
        # No network-capable production command may survive this fixture rewrite.
        for command in ["route", "scutil", "netstat", "ifconfig", "ipconfig", "dscacheutil", "curl", "killall", "networksetup", "ndp"]:
            self.assertNotRegex(script, r"/(?:usr/)?s?bin/" + command + r"\b")
        self.script = self.root / "guard.sh"
        self.script.write_text(script)
        (self.root / "config").write_text("MODE=repair\n")
        (self.root / "zt.conf").write_text("allowDefault=0\n")
        (self.root / "state").mkdir()
        (self.root / "state/state").write_text("owned_routes=1\nowned_interface=en1\nowned_gateway=192.168.1.1\n")
        self.data = {}

    def run_guard(self, **updates):
        self.data.update(updates)
        (self.root / "fixture.json").write_text(json.dumps(self.data))
        start = time.monotonic()
        p = subprocess.run(["/bin/bash", str(self.script)], capture_output=True, text=True, timeout=25)
        self.elapsed = time.monotonic() - start
        self.assertEqual(p.returncode, 0, p.stderr)
        self.data = json.loads((self.root / "fixture.json").read_text())
        state = dict(line.split("=", 1) for line in (self.root / "state/state").read_text().splitlines())
        self.assertFalse((self.root / "lock").exists(), "cycle must release its lock")
        return state

    def writes(self):
        path = self.root / "writes.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def test_owned_stale_ifa_same_gateway_interface_and_idempotence(self):
        state = self.run_guard(low_ifa="192.168.1.9", high_ifa="192.168.1.9")
        self.assertEqual(state["decision"], "repaired_both")
        self.assertEqual(state["low_ifa"], "192.168.1.7")
        self.assertEqual(state["high_ifa"], "192.168.1.7")
        self.assertEqual(state["zt_route_interface"], "feth1924")
        self.assertEqual(len(self.writes()), 2)
        state = self.run_guard()
        self.assertEqual(state["decision"], "protected_by_physical_split_routes")
        self.assertEqual(len(self.writes()), 2)

    def test_ifa_refresh_independent_of_zt_bad_keys(self):
        self.assertEqual(self.run_guard(low_ifa="192.168.1.9", zt_clean=True)["decision"], "repaired_low")
        self.assertEqual(len(self.writes()), 1)

    def test_clean_zt_does_not_authorize_generic_repair(self):
        state = self.run_guard(zt_clean=True, low_missing=True, high_missing=True,
                               low_sentinel_gateway="127.0.0.1", low_sentinel_iface="lo0",
                               high_sentinel_gateway="127.0.0.1", high_sentinel_iface="lo0")
        self.assertEqual(state["decision"], "zt_state_not_bad")
        self.assertEqual(self.writes(), [])

    def test_foreign_sentinel_is_not_overridden_by_owned_stale_split(self):
        state = self.run_guard(low_ifa="192.168.1.9", low_sentinel_gateway="10.2.3.1",
                               low_sentinel_iface="utun6")
        self.assertEqual(state["decision"], "observe_nonphysical_route_active")
        self.assertEqual(self.writes(), [])

    def test_dynamic_physical_address(self):
        state = self.run_guard(address="192.168.1.12", low_ifa="192.168.1.7", high_ifa="192.168.1.7")
        self.assertEqual(state["decision"], "repaired_both")
        self.assertTrue(all(row[1][-1] == "192.168.1.12" for row in self.writes()))

    def test_healthy_idempotent(self):
        self.assertEqual(self.run_guard()["decision"], "protected_by_physical_split_routes")
        self.assertEqual(self.writes(), [])

    def test_unowned_stale_not_adopted(self):
        (self.root / "state/state").write_text("owned_routes=0\n")
        self.assertEqual(self.run_guard(low_ifa="192.168.1.9")["decision"], "observe_unowned_stale_ifa")
        self.assertEqual(self.writes(), [])

    def test_vpn_conflict_no_mutation(self):
        self.assertEqual(self.run_guard(low_ifa="192.168.1.9", vpn=True, dns_bad=True)["decision"], "observe_vpn_or_split_route_conflict")
        self.assertEqual(self.writes(), [])

    def test_remote_entry_gate(self):
        self.assertEqual(self.run_guard(zt_iface="en1", low_ifa="192.168.1.9")["decision"], "zt_route_not_target_interface")
        self.assertEqual(self.writes(), [])

    def test_registered_unplugged_adapter_is_not_a_probe_failure(self):
        self.assertEqual(self.run_guard(absent_service=True)["decision"], "protected_by_physical_split_routes")
        self.assertEqual(self.writes(), [])

    def test_registered_unplugged_adapter_does_not_block_dns_heal(self):
        state = self.run_guard(absent_service=True, dns_bad=True)
        self.assertEqual(state["decision"], "repaired_dns_stripped")
        self.assertEqual([x[0] for x in self.writes()], ["scutil"])

    def test_interface_inventory_failure_closes_gate(self):
        self.assertEqual(self.run_guard(fail="ifconfig")["decision"], "probe_failed")
        self.assertEqual(self.writes(), [])

    def test_ambiguous_physical_gate(self):
        self.assertEqual(self.run_guard(two_physical=True, low_ifa="192.168.1.9")["decision"], "observe_physical_route_ambiguous")
        self.assertEqual(self.writes(), [])

    def test_observe_does_not_strip_dns(self):
        (self.root / "config").write_text("MODE=observe\n")
        self.assertEqual(self.run_guard(dns_bad=True)["dns_status"], "would_strip")
        self.assertEqual(self.writes(), [])

    def test_observe_does_not_change_stale_routes(self):
        (self.root / "config").write_text("MODE=observe\n")
        self.assertEqual(self.run_guard(low_ifa="192.168.1.9")["decision"], "observe_would_repair_low")
        self.assertEqual(self.writes(), [])

    def test_observe_preserves_ipv6_fallback_without_writing(self):
        (self.root / "config").write_text("MODE=observe\nIPV6_REPAIR_INTERFACE=en1\nIPV6_REPAIR_GATEWAY=fe80::1\nIPV6_REPAIR_IPV4_GATEWAY=192.168.1.1\n")
        self.assertEqual(self.run_guard(ipv6_missing=True)["ipv6_repair_status"], "would_restore")
        self.assertEqual(self.writes(), [])

    def test_existing_ipv6_default_is_preserved(self):
        (self.root / "config").write_text("MODE=repair\nIPV6_REPAIR_INTERFACE=en1\nIPV6_REPAIR_GATEWAY=fe80::1\nIPV6_REPAIR_IPV4_GATEWAY=192.168.1.1\n")
        self.assertEqual(self.run_guard()["ipv6_repair_status"], "default_present")
        self.assertEqual(self.writes(), [])

    def test_route_timeout_is_bounded_and_next_cycle_runs(self):
        self.assertEqual(self.run_guard(hang="route")["decision"], "probe_failed")
        self.assertLess(self.elapsed, 7)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.run_guard(hang=None)["decision"], "protected_by_physical_split_routes")

    def test_dns_acceptance_timeout(self):
        self.assertEqual(self.run_guard(dns_bad=True, hang="dscacheutil")["decision"], "probe_failed")
        self.assertLess(self.elapsed, 15)
        self.assertEqual([x[0] for x in self.writes()], ["scutil"])
        self.assertEqual(self.run_guard(hang=None)["decision"], "protected_by_physical_split_routes")

    def test_netstat_error_is_not_an_empty_table(self):
        self.assertEqual(self.run_guard(fail="netstat", low_ifa="192.168.1.9")["decision"], "probe_failed")
        self.assertEqual(self.writes(), [])

    def test_duplicate_split_is_not_owned(self):
        self.assertEqual(self.run_guard(duplicate=True, low_ifa="192.168.1.9")["decision"], "probe_failed")
        self.assertEqual(self.writes(), [])

    def test_unknown_ifa_is_not_repaired(self):
        self.assertEqual(self.run_guard(unknown_ifa=True)["decision"], "probe_failed")
        self.assertEqual(self.writes(), [])

    def test_mutation_nonstandard_exit_is_a_failure(self):
        self.assertEqual(self.run_guard(low_ifa="192.168.1.9", mutation_fail=True)["decision"], "repair_route_command_failed")


if __name__ == "__main__":
    unittest.main()
