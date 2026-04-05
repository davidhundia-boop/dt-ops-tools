"""Patch split-required APKs so base-only APKs install without their splits.

Removes `android:requiredSplitTypes` from the binary AndroidManifest.xml,
then re-signs using Android SDK build-tools (zipalign + apksigner).
Falls back to pure-Python JAR v1 signing if SDK tools are unavailable.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile

_SPLIT_TYPE_STRINGS = [
    "base__abi",
    "base__density",
    "base__locale",
]

_SDK_BUILD_TOOLS: str | None = None
_JAVA_HOME: str | None = None


def _find_sdk_tools() -> tuple[str | None, str | None]:
    """Locate Android SDK build-tools and a JDK (from Android Studio)."""
    global _SDK_BUILD_TOOLS, _JAVA_HOME
    if _SDK_BUILD_TOOLS is not None:
        return _SDK_BUILD_TOOLS, _JAVA_HOME

    sdk_root = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if not sdk_root:
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Android\Sdk"),
            os.path.expanduser("~/Library/Android/sdk"),
            os.path.expanduser("~/Android/Sdk"),
        ]
        for c in candidates:
            if os.path.isdir(c):
                sdk_root = c
                break

    if sdk_root:
        bt_dir = os.path.join(sdk_root, "build-tools")
        if os.path.isdir(bt_dir):
            versions = sorted(os.listdir(bt_dir), reverse=True)
            for v in versions:
                apksigner = os.path.join(bt_dir, v, "apksigner.bat" if sys.platform == "win32" else "apksigner")
                if os.path.isfile(apksigner):
                    _SDK_BUILD_TOOLS = os.path.join(bt_dir, v)
                    break

    for studio_path in [
        r"C:\Program Files\Android\Android Studio\jbr",
        r"C:\Program Files\Android\Android Studio\jre",
        "/Applications/Android Studio.app/Contents/jbr/Contents/Home",
        "/Applications/Android Studio.app/Contents/jre/Contents/Home",
    ]:
        java_exe = os.path.join(studio_path, "bin", "java.exe" if sys.platform == "win32" else "java")
        if os.path.isfile(java_exe):
            _JAVA_HOME = studio_path
            break

    return _SDK_BUILD_TOOLS, _JAVA_HOME


# ---------------------------------------------------------------------------
# Binary AXML patching
# ---------------------------------------------------------------------------

def _patch_axml_string(manifest: bytearray, target_str: str) -> bool:
    encoded = target_str.encode("utf-16-le")
    idx = manifest.find(encoded)
    if idx < 0:
        return False
    length_offset = idx - 2
    char_count = struct.unpack_from("<H", manifest, length_offset)[0]
    if char_count != len(target_str):
        return False
    struct.pack_into("<H", manifest, length_offset, 0)
    struct.pack_into("<H", manifest, idx, 0)
    return True


def patch_manifest(manifest_bytes: bytes) -> bytes | None:
    buf = bytearray(manifest_bytes)
    patched_any = False
    for s in _SPLIT_TYPE_STRINGS:
        if _patch_axml_string(buf, s):
            patched_any = True
    return bytes(buf) if patched_any else None


# ---------------------------------------------------------------------------
# Signing helpers
# ---------------------------------------------------------------------------

def _ensure_debug_keystore() -> str:
    ks_path = os.path.join(os.path.expanduser("~"), ".android", "debug.keystore")
    if os.path.isfile(ks_path):
        return ks_path

    os.makedirs(os.path.dirname(ks_path), exist_ok=True)
    _, java_home = _find_sdk_tools()
    keytool = "keytool"
    if java_home:
        keytool = os.path.join(java_home, "bin", "keytool.exe" if sys.platform == "win32" else "keytool")

    subprocess.run([
        keytool, "-genkey", "-v",
        "-keystore", ks_path,
        "-storepass", "android",
        "-alias", "androiddebugkey",
        "-keypass", "android",
        "-keyalg", "RSA",
        "-keysize", "2048",
        "-validity", "10000",
        "-dname", "CN=Android Debug,O=Android,C=US",
    ], check=True, capture_output=True, timeout=30)
    return ks_path


def _sign_with_sdk(apk_path: str) -> bool:
    """zipalign + apksigner using Android SDK build-tools. Returns True on success."""
    bt, java_home = _find_sdk_tools()
    if not bt or not java_home:
        return False

    env = os.environ.copy()
    env["JAVA_HOME"] = java_home
    env["PATH"] = os.path.join(java_home, "bin") + os.pathsep + env.get("PATH", "")

    aligned = apk_path + ".aligned"
    zipalign = os.path.join(bt, "zipalign.exe" if sys.platform == "win32" else "zipalign")
    r = subprocess.run([zipalign, "-f", "4", apk_path, aligned],
                       capture_output=True, text=True, timeout=60, env=env)
    if r.returncode != 0:
        return False
    shutil.move(aligned, apk_path)

    ks_path = _ensure_debug_keystore()
    apksigner = os.path.join(bt, "apksigner.bat" if sys.platform == "win32" else "apksigner")
    r = subprocess.run([
        apksigner, "sign",
        "--ks", ks_path,
        "--ks-pass", "pass:android",
        apk_path,
    ], capture_output=True, text=True, timeout=60, env=env)
    return r.returncode == 0


def _sign_with_python(apk_path: str) -> bool:
    """Fallback: JAR v1 signing using pure Python (cryptography). Won't work on API 30+."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import Encoding, pkcs7
        from cryptography.x509.oid import NameOID
        import hashlib
        import base64
        from datetime import datetime, timezone
    except ImportError:
        return False

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Android Debug"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Android"),
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime(2024, 1, 1, tzinfo=timezone.utc))
        .not_valid_after(datetime(2034, 1, 1, tzinfo=timezone.utc))
        .sign(key, hashes.SHA256())
    )

    def b64d(data: bytes) -> str:
        return base64.b64encode(hashlib.sha256(data).digest()).decode()

    tmp = apk_path + ".tmp"
    with zipfile.ZipFile(apk_path, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        entries: dict[str, bytes] = {}
        for item in zin.infolist():
            if item.filename.startswith("META-INF/"):
                continue
            data = zin.read(item.filename)
            zout.writestr(item, data)
            entries[item.filename] = data

        mf_lines = ["Manifest-Version: 1.0", "Created-By: patch_apk.py", ""]
        for name in sorted(entries):
            mf_lines += [f"Name: {name}", f"SHA-256-Digest: {b64d(entries[name])}", ""]
        manifest_mf = "\r\n".join(mf_lines).encode()

        sf_lines = [
            "Signature-Version: 1.0",
            f"SHA-256-Digest-Manifest: {b64d(manifest_mf)}",
            "Created-By: patch_apk.py", "",
        ]
        for section in manifest_mf.split(b"\r\n\r\n"):
            if not section.strip():
                continue
            for line in section.split(b"\r\n"):
                if line.startswith(b"Name: "):
                    sf_lines += [
                        f"Name: {line.decode()[6:]}",
                        f"SHA-256-Digest: {b64d(section + b'\r\n\r\n')}", "",
                    ]
                    break
        cert_sf = "\r\n".join(sf_lines).encode()

        cert_rsa = (
            pkcs7.PKCS7SignatureBuilder()
            .set_data(cert_sf)
            .add_signer(cert, key, hashes.SHA256())
            .sign(Encoding.DER, [
                pkcs7.PKCS7Options.DetachedSignature,
                pkcs7.PKCS7Options.Binary,
                pkcs7.PKCS7Options.NoCapabilities,
            ])
        )
        zout.writestr("META-INF/MANIFEST.MF", manifest_mf)
        zout.writestr("META-INF/CERT.SF", cert_sf)
        zout.writestr("META-INF/CERT.RSA", cert_rsa)

    shutil.move(tmp, apk_path)
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def needs_split_patch(apk_path: str) -> bool:
    """Check whether the APK has a requiredSplitTypes attribute that blocks install."""
    with zipfile.ZipFile(apk_path, "r") as z:
        manifest = z.read("AndroidManifest.xml")
    return any(s.encode("utf-16-le") in manifest for s in _SPLIT_TYPE_STRINGS)


def patch_apk(src_path: str, dst_path: str | None = None) -> str:
    """Create a patched + re-signed copy of an APK with split requirements removed.

    Returns the path to the patched APK, or src_path unchanged if no patching needed.
    """
    if dst_path is None:
        base, ext = os.path.splitext(src_path)
        dst_path = f"{base}_patched{ext}"

    with zipfile.ZipFile(src_path, "r") as zin:
        manifest = zin.read("AndroidManifest.xml")
        patched = patch_manifest(manifest)

        if patched is None:
            if src_path != dst_path:
                shutil.copy2(src_path, dst_path)
            return dst_path

        with zipfile.ZipFile(dst_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename.startswith("META-INF/"):
                    continue
                data = zin.read(item.filename)
                if item.filename == "AndroidManifest.xml":
                    data = patched
                zout.writestr(item, data)

    if _sign_with_sdk(dst_path):
        return dst_path

    if _sign_with_python(dst_path):
        return dst_path

    raise RuntimeError(
        "Cannot re-sign the patched APK. Install Android SDK build-tools "
        "or the Python 'cryptography' package."
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python patch_apk.py <input.apk> [output.apk]")
        sys.exit(1)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else None
    result = patch_apk(src, dst)
    print(f"Patched APK: {result}")
