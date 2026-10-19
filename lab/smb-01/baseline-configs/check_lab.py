"""Kiem tra tinh nhat quan cua bo cau hinh smb-01 (khong can thiet bi that).

- Moi thiet bi co dinh tuyen (router, hoac switch co 'ip routing') va moi route tinh.
- Next-hop cua route tinh phai nam trong mot subnet noi truc tiep (bang global).
- VLAN dung o access / trunk allowed / SVI phai duoc khai bao.
- Mo phong chuyen tiep hop-by-hop giua cac subnet LAN va ISP, ca chieu di va chieu ve.
- Trong cung interface, khong lenh nao (tru description) dung truoc 'no switchport'.
"""

import ipaddress
import pathlib
import re
import sys

ROUTERS = {"R-EDGE1", "R-BR1", "ISP"}


def parse(path):
    text = path.read_text()
    dev = {
        "name": path.stem,
        "intfs": {},
        "routes": [],
        "vlans": set(),
        "used_vlans": set(),
        "ip_routing": None,
    }
    dev["ip_routing"] = (path.stem in ROUTERS) or bool(re.search(r"^ip routing$", text, re.M))
    cur = None
    for line in text.splitlines():
        if line.startswith("interface "):
            cur = line.split()[1]
            dev["intfs"][cur] = {"ip": None, "vrf": None, "shutdown": False, "switchport": True}
            continue
        if not line.startswith(" "):
            cur = None
        if cur:
            s = line.strip()
            if s.startswith("ip address "):
                _, _, a, m = s.split()[:4]
                dev["intfs"][cur]["ip"] = ipaddress.ip_interface(f"{a}/{m}")
            elif s.startswith("vrf forwarding"):
                dev["intfs"][cur]["vrf"] = s.split()[-1]
            elif s == "shutdown":
                dev["intfs"][cur]["shutdown"] = True
            elif s.startswith("switchport access vlan"):
                dev["used_vlans"].add(int(s.split()[-1]))
            elif s.startswith("switchport trunk allowed vlan"):
                for v in s.split()[-1].split(","):
                    dev["used_vlans"].add(int(v))
            elif s.startswith("switchport trunk native vlan"):
                dev["used_vlans"].add(int(s.split()[-1]))
        m = re.match(r"^vlan (\d+)$", line)
        if m:
            dev["vlans"].add(int(m.group(1)))
        m = re.match(r"^ip route (\S+) (\S+) (\S+)", line)
        if m:
            dev["routes"].append(
                (ipaddress.ip_network(f"{m.group(1)}/{m.group(2)}"), ipaddress.ip_address(m.group(3)))
            )
    for name in dev["intfs"]:
        if name.startswith("Vlan"):
            dev["used_vlans"].add(int(name[4:]))
    return dev


def before_no_switchport(text):
    """(interface, lenh) cho moi lenh dung truoc 'no switchport' trong cung interface, tru description.

    'no switchport' dua interface ve mac dinh nen IOS xoa cac lenh da nap truoc no. Da gap tren lab
    (13/10/2026): 'no cdp enable' dat truoc 'no switchport' o cong quan ly, CDP van chay qua mang OOB.
    """
    found, cur, seen = [], None, []
    for line in text.splitlines():
        if line.startswith("interface "):
            cur, seen = line.split()[1], []
            continue
        if not line.startswith(" "):
            cur = None
        if cur and seen is not None:
            s = line.strip()
            if s == "no switchport":
                found += [(cur, cmd) for cmd in seen]
                seen = None
            elif not s.startswith("description "):
                seen.append(s)
    return found


def connected(dev):
    return [
        (i["ip"].network, n)
        for n, i in dev["intfs"].items()
        if i["ip"] and not i["shutdown"] and i["vrf"] is None
    ]


def lookup(dev, dst):
    best = None
    for net, nh in [(n, None) for n, _ in connected(dev)] + dev["routes"]:
        if dst in net and (best is None or net.prefixlen > best[0].prefixlen):
            best = (net, nh)
    return best


def owner(devs, ip):
    for d in devs.values():
        for i in d["intfs"].values():
            if i["ip"] and i["ip"].ip == ip and i["vrf"] is None:
                return d
    return None


def forward(devs, start, dst, limit=10):
    path, cur = [start["name"]], start
    for _ in range(limit):
        if any(i["ip"] and i["ip"].ip == dst for i in cur["intfs"].values()):
            return True, path
        if not cur["ip_routing"]:
            # switch L2: chi gui toi subnet noi truc tiep
            if not any(dst in n for n, _ in connected(cur)):
                return False, path + ["(khong dinh tuyen)"]
        hit = lookup(cur, dst)
        if hit is None:
            return False, path + ["(khong co route)"]
        net, nh = hit
        if nh is None:
            nxt = owner(devs, dst)
            return (nxt is not None), path + ([nxt["name"]] if nxt else ["(khong ai so huu)"])
        nxt = owner(devs, nh)
        if nxt is None:
            return False, path + [f"(next-hop {nh} khong ton tai)"]
        cur = nxt
        path.append(cur["name"])
    return False, path + ["(vong lap)"]


def main(folder):
    devs = {p.stem: parse(p) for p in sorted(pathlib.Path(folder).glob("*.cfg"))}
    problems = 0
    for p in sorted(pathlib.Path(folder).glob("*.cfg")):
        for intf, cmd in before_no_switchport(p.read_text()):
            print(f"[THU-TU] {p.stem} {intf}: '{cmd}' dung truoc 'no switchport' (IOS xoa lenh nay)")
            problems += 1
    for d in devs.values():
        miss = d["used_vlans"] - d["vlans"] - {1}
        if miss and d["name"] not in ROUTERS:
            print(f"[VLAN] {d['name']}: dung nhung chua khai bao {sorted(miss)}")
            problems += 1
        if d["routes"] and not d["ip_routing"]:
            print(f"[ROUTING] {d['name']}: co route tinh nhung khong co ip routing")
            problems += 1
        for net, nh in d["routes"]:
            if not any(nh in n for n, _ in connected(d)):
                print(f"[NEXT-HOP] {d['name']}: {net} via {nh} khong nam trong subnet noi truc tiep")
                problems += 1
    tests = [("CORE1", "10.10.10.1"), ("ACC-BR1", "10.20.10.1"), ("ISP", "198.51.100.1")]
    srcs = {"CORE1": "10.10.10.1", "ACC-BR1": "10.20.10.1", "ISP": "198.51.100.1"}
    for a, _ in tests:
        for b, dst in tests:
            if a == b:
                continue
            ok, path = forward(devs, devs[a], ipaddress.ip_address(dst))
            okr, pathr = forward(devs, devs[b], ipaddress.ip_address(srcs[a]))
            mark = "OK " if ok and okr else "LOI"
            if mark == "LOI":
                problems += 1
            print(f"[{mark}] {a} -> {b}: di {' > '.join(path)} | ve {' > '.join(pathr)}")
    print(f"\nTong so van de: {problems}")
    return problems


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1]) else 0)
