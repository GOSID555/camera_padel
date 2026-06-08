"""ค้นหากล้อง IP/CCTV บนวง LAN ด้วย ONVIF แล้วดึง RTSP URL อัตโนมัติ

- discover()      : ยิง WS-Discovery (UDP multicast) หากล้องทุกตัว — ไม่ต้องใช้รหัสผ่าน
- get_rtsp_url()  : ถาม ONVIF (GetCapabilities→GetProfiles→GetStreamUri) เอาลิงก์ RTSP
                    ที่ถูกต้องของกล้อง โดยใช้ user/pass — ไม่ต้องเดา path เอง

ใช้แค่ stdlib + httpx (มีใน requirements อยู่แล้ว) ไม่ต้องลง onvif lib เพิ่ม
"""
import base64
import hashlib
import os
import re
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

_WSD_ADDR = ("239.255.255.250", 3702)
# กล้องบางรุ่น (เช่น TP-Link VIGI) ตั้งบัญชี ONVIF แยกจาก login/RTSP ปกติ — ต้องเปิด/
# ตั้งรหัส ONVIF ในตัวกล้องก่อน รหัส RTSP เดิมใช้กับ ONVIF ไม่ได้
_AUTH_MSG = ("ONVIF ปฏิเสธรหัส — กล้องบางรุ่น (เช่น VIGI) ใช้บัญชี ONVIF แยกจากรหัส "
             "login/RTSP ปกติ ลองเปิด/ตั้งบัญชี ONVIF ในตัวกล้องแล้วใส่รหัสนั้น")


def _unauth(xml: str) -> bool:
    return "NotAuthorized" in xml or "Sender not Authorized" in xml or "Authority failure" in xml
_TT = "http://www.onvif.org/ver10/schema"          # common types
_TDS = "http://www.onvif.org/ver10/device/wsdl"    # device service
_TRT = "http://www.onvif.org/ver10/media/wsdl"     # media service


# ── 1) Discovery (no credentials) ─────────────────────────────────────────────

def discover(timeout: float = 4.0) -> list[dict]:
    """คืน list ของ {ip, xaddr, name, model} จากกล้อง ONVIF ทุกตัวบนวง LAN"""
    probe = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        f'<e:Header><w:MessageID>uuid:{uuid.uuid4()}</w:MessageID>'
        '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        '<w:Action e:mustUnderstand="true">'
        'http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>'
        '<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>'
        '</e:Envelope>'
    )
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(timeout)
    try:
        s.sendto(probe.encode(), _WSD_ADDR)
    except OSError:
        s.close()
        return []

    found: dict[str, dict] = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = s.recvfrom(65535)
        except socket.timeout:
            break
        except OSError:
            break
        ip = addr[0]
        text = data.decode(errors="replace")
        xa = re.search(r"<[^>]*XAddrs>(.*?)</[^>]*XAddrs>", text, re.S)
        sc = re.search(r"<[^>]*Scopes>(.*?)</[^>]*Scopes>", text, re.S)
        xaddr = xa.group(1).split()[0].strip() if xa and xa.group(1).split() else ""
        scopes = sc.group(1) if sc else ""
        name = re.search(r"onvif://www\.onvif\.org/name/([^\s<]+)", scopes)
        hw = re.search(r"onvif://www\.onvif\.org/hardware/([^\s<]+)", scopes)
        found[ip] = {
            "ip": ip,
            "xaddr": xaddr,
            "name": _unesc(name.group(1)) if name else ip,
            "model": _unesc(hw.group(1)) if hw else "",
        }
    s.close()
    return list(found.values())


def _unesc(s: str) -> str:
    # scope ค่าจะ url-encode ช่องว่าง/อักขระพิเศษ เช่น VIGI%20C385
    from urllib.parse import unquote
    return unquote(s)


# ── 1b) Fallback: สแกนพอร์ตทั้ง subnet ────────────────────────────────────────
# กล้องบางตัวปิด ONVIF WS-Discovery หรือ router บล็อก multicast → discover() ไม่เจอ
# แต่กล้องเกือบทุกตัวยังเปิดพอร์ต RTSP (554) ค้างไว้ → ไล่ TCP connect ทุก host
# ในวง /24 หา host ที่เปิด 554 = น่าจะเป็นกล้อง แล้วเดา ONVIF service URL ให้ด้วย
# (ถ้าเปิดพอร์ต ONVIF) เพื่อให้ get_rtsp_url() ดึงลิงก์อัตโนมัติต่อได้เหมือน discover()

_RTSP_PORTS = (554, 8554)
# เรียงตามลำดับความน่าจะเป็น ONVIF service: VIGI=2020, กล้องจีนหลายรุ่น=8899/8000, อื่นๆ=80
_ONVIF_PORTS = (2020, 8000, 8899, 8080, 80)


def _port_open(ip: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _local_ipv4s() -> list[str]:
    """IPv4 ของเครื่องนี้ทุกใบ (เอาไว้หา subnet ที่จะสแกน) ข้าม loopback"""
    ips: set[str] = set()
    # IP ของ default route — ใบที่ต่อกับกล้องจริงๆ
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return [ip for ip in ips if not ip.startswith("127.")]


def scan_subnet(timeout: float = 0.4, concurrency: int = 100) -> list[dict]:
    """ไล่สแกนทุก host ใน /24 ของทุก interface — คืน list เดียวกับ discover()
    {ip, xaddr, name, model} + ports/source เพิ่ม. ใช้เป็น fallback ตอน discover() ว่าง"""
    own = set(_local_ipv4s())
    # /24 ต่อหนึ่ง interface (เรียง default-route subnet ไว้ก่อน)
    prefixes: list[str] = []
    for ip in own:
        pfx = ip.rsplit(".", 1)[0]
        if pfx not in prefixes:
            prefixes.append(pfx)
    targets = [f"{pfx}.{h}" for pfx in prefixes for h in range(1, 255)
               if f"{pfx}.{h}" not in own]

    def probe(ip: str) -> dict | None:
        rtsp_open = [p for p in _RTSP_PORTS if _port_open(ip, p, timeout)]
        if not rtsp_open:
            return None                       # ไม่เปิด RTSP → ไม่ใช่กล้อง (กรอง router/PC ออก)
        xaddr = ""
        for p in _ONVIF_PORTS:                # เดา ONVIF service URL เพื่อ auto-ดึง RTSP ต่อได้
            if _port_open(ip, p, timeout):
                xaddr = f"http://{ip}:{p}/onvif/device_service"
                break
        return {"ip": ip, "xaddr": xaddr, "name": ip, "model": "",
                "ports": rtsp_open, "source": "ip-scan"}

    found: list[dict] = []
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        for r in ex.map(probe, targets):
            if r:
                found.append(r)
    return found


# ── 2) ดึง RTSP URL จริงผ่าน ONVIF (ต้องใช้ user/pass) ─────────────────────────

def _security_header(user: str, password: str) -> str:
    """WS-Security UsernameToken แบบ PasswordDigest — กล้อง ONVIF ส่วนใหญ่ต้องการ"""
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    ).decode()
    nonce_b64 = base64.b64encode(nonce).decode()
    wsse = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
    wsu = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
    return (
        f'<s:Header><Security s:mustUnderstand="1" xmlns="{wsse}">'
        f'<UsernameToken><Username>{_xml(user)}</Username>'
        f'<Password Type="{wsse}#PasswordDigest">{digest}</Password>'
        f'<Nonce EncodingType="{wsse}#Base64Binary">{nonce_b64}</Nonce>'
        f'<Created xmlns="{wsu}">{created}</Created>'
        f'</UsernameToken></Security></s:Header>'
    )


def _xml(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _soap(url: str, body: str, user: str, password: str, timeout: float = 6.0) -> str:
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        f'{_security_header(user, password)}'
        f'<s:Body>{body}</s:Body></s:Envelope>'
    )
    r = httpx.post(url, content=envelope.encode(),
                   headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                   timeout=timeout)
    return r.text


def get_rtsp_url(xaddr: str, user: str, password: str, ip: str = "") -> str:
    """คืน RTSP URL พร้อม user:pass ฝังในลิงก์ — raise RuntimeError ถ้าล้มเหลว

    xaddr = ONVIF device service URL จาก discover() เช่น http://IP:2020/onvif/device_service
    """
    if not xaddr:
        raise RuntimeError("ไม่มี ONVIF service URL ของกล้องนี้")

    # (1) หา Media service URL
    caps = _soap(xaddr,
                 f'<tds:GetCapabilities xmlns:tds="{_TDS}">'
                 f'<tds:Category>Media</tds:Category></tds:GetCapabilities>',
                 user, password)
    m = re.search(r"<[^>]*Media>.*?<[^>]*XAddr>(.*?)</[^>]*XAddr>", caps, re.S)
    if not m:
        if _unauth(caps):
            raise RuntimeError(_AUTH_MSG)
        raise RuntimeError("กล้องไม่ตอบ ONVIF Media (อาจไม่รองรับ)")
    media_url = m.group(1).strip()

    # (2) เอา profile token ตัวแรก
    profs = _soap(media_url, f'<trt:GetProfiles xmlns:trt="{_TRT}"/>', user, password)
    tok = re.search(r'token="([^"]+)"', profs) or re.search(r"token='([^']+)'", profs)
    if not tok:
        if _unauth(profs):
            raise RuntimeError(_AUTH_MSG)
        raise RuntimeError("ไม่พบโปรไฟล์สตรีมในกล้อง")
    token = tok.group(1)

    # (3) GetStreamUri (RTP-Unicast over RTSP)
    stream = _soap(
        media_url,
        f'<trt:GetStreamUri xmlns:trt="{_TRT}" xmlns:tt="{_TT}">'
        '<trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>'
        '<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>'
        f'<trt:ProfileToken>{_xml(token)}</trt:ProfileToken></trt:GetStreamUri>',
        user, password)
    u = re.search(r"<[^>]*Uri>\s*(rtsp[^<]+)\s*</[^>]*Uri>", stream)
    if not u:
        if _unauth(stream):
            raise RuntimeError(_AUTH_MSG)
        raise RuntimeError("กล้องไม่คืน RTSP URL")
    return _inject_creds(u.group(1).strip(), user, password, ip)


def _inject_creds(uri: str, user: str, password: str, ip: str = "") -> str:
    """ฝัง user:pass ลงใน rtsp URL และแก้ host ให้เป็น IP ที่สแกนเจอ (กันกล้องคืน hostname)"""
    parts = urlsplit(uri)
    host = ip or parts.hostname or ""
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
