from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "TriAevumKOR Fresh"
MEDIA_UNIT = 0x200
KOR_TITLE_ID = 0x000400000008F800
KOR_TITLE_ID_TEXT = "000400000008F800"
KOR_PRODUCT_CODE = "CTR-P-AQEK"
COPY_CHUNK = 4 * 1024 * 1024
MAX_CODE = 512 * 1024 * 1024


class RomError(RuntimeError):
    pass


@dataclass
class Layout:
    container_kind: str
    partition_index: int
    partition_base: int
    partition_end: int
    program_id: int
    product_code: str
    compressed_code: bool
    exheader_offset: int
    code_offset: int
    code_size: int
    romfs_offset: int
    romfs_size: int


@dataclass
class Extracted:
    path: Path
    bytes: int
    sha256: str


def u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def u64(data: bytes, off: int) -> int:
    return struct.unpack_from("<Q", data, off)[0]


def read_exact(f, off: int, size: int, label: str) -> bytes:
    f.seek(off)
    data = f.read(size)
    if len(data) != size:
        raise RomError(f"{label} 읽기 중 파일이 끝났습니다.")
    return data


def checked(off: int, size: int, lo: int, hi: int, label: str):
    if off < lo or size <= 0 or off > hi or size > hi - off:
        raise RomError(f"{label} 위치가 ROM 범위를 벗어났습니다.")


def parse_ncch(f, file_size: int, base: int, size: int, index: int, kind: str) -> Layout:
    end = base + size
    checked(base, 0x200, 0, file_size, "NCCH 헤더")
    if end > file_size:
        raise RomError("NCCH 파티션 크기가 ROM 크기보다 큽니다.")

    h = read_exact(f, base, 0x200, "NCCH 헤더")
    if h[0x100:0x104] != b"NCCH":
        raise RomError("NCCH 파티션이 아닙니다.")
    if not (h[0x18D] & 0x02):
        raise RomError("실행 가능한 애플리케이션 파티션이 아닙니다.")
    if u32(h, 0x180) == 0:
        raise RomError("ExHeader가 없습니다.")

    exheader_off = base + 0x200
    checked(exheader_off, 0x800, base, end, "ExHeader")
    exheader = read_exact(f, exheader_off, 0x800, "ExHeader")

    exefs_units = u32(h, 0x1A0)
    exefs_size_units = u32(h, 0x1A4)
    if not exefs_units or not exefs_size_units:
        raise RomError("ExeFS가 없습니다.")
    exefs_off = base + exefs_units * MEDIA_UNIT
    exefs_size = exefs_size_units * MEDIA_UNIT
    checked(exefs_off, exefs_size, base, end, "ExeFS")
    exefs_h = read_exact(f, exefs_off, 0x200, "ExeFS 헤더")

    code_off = None
    code_size = 0
    for i in range(8):
        e = i * 0x10
        name = exefs_h[e:e+8].split(b"\0", 1)[0]
        if name == b".code":
            rel = u32(exefs_h, e + 8)
            code_size = u32(exefs_h, e + 12)
            code_off = exefs_off + 0x200 + rel
            checked(code_off, code_size, exefs_off + 0x200, exefs_off + exefs_size, "ExeFS .code")
            break
    if code_off is None:
        raise RomError(
            "ExeFS .code를 읽을 수 없습니다. ROM이 암호화되어 있거나 지원되지 않는 덤프일 수 있습니다."
        )

    romfs_units = u32(h, 0x1B0)
    romfs_size_units = u32(h, 0x1B4)
    if not romfs_units or not romfs_size_units:
        raise RomError("RomFS가 없습니다.")
    romfs_off = base + romfs_units * MEDIA_UNIT
    romfs_size = romfs_size_units * MEDIA_UNIT
    checked(romfs_off, romfs_size, base, end, "RomFS")
    if read_exact(f, romfs_off, 4, "RomFS 헤더") != b"IVFC":
        raise RomError(
            "RomFS가 복호화된 IVFC 형식이 아닙니다. 복호화된 .3ds/.cci ROM이 필요합니다."
        )

    product = h[0x150:0x160].split(b"\0", 1)[0].decode("ascii", "replace")
    return Layout(
        kind, index, base, end, u64(h, 0x118), product,
        bool(exheader[0x0D] & 1), exheader_off,
        code_off, code_size, romfs_off, romfs_size
    )


def find_layout(path: Path) -> Layout:
    size = path.stat().st_size
    with path.open("rb") as f:
        h = read_exact(f, 0, 0x200, "컨테이너 헤더")
        magic = h[0x100:0x104]
        if magic == b"NCCH":
            return parse_ncch(f, size, 0, size, 0, "NCCH")
        if magic != b"NCSD":
            raise RomError("Nintendo 3DS NCSD/NCCH ROM이 아닙니다.")

        errors = []
        for i in range(8):
            e = 0x120 + i * 8
            base = u32(h, e) * MEDIA_UNIT
            psize = u32(h, e + 4) * MEDIA_UNIT
            if not base or not psize:
                continue
            try:
                return parse_ncch(f, size, base, psize, i, "NCSD")
            except RomError as exc:
                errors.append(str(exc))
        raise RomError("사용 가능한 복호화 애플리케이션 파티션을 찾지 못했습니다.\n" + "\n".join(errors[:4]))


def decompress_code(data: bytes) -> bytes:
    if len(data) < 8:
        raise RomError("압축 .code가 너무 작습니다.")
    top_bottom = u32(data, len(data) - 8)
    extra = u32(data, len(data) - 4)
    out_size = len(data) + extra
    if out_size < len(data) or out_size > MAX_CODE:
        raise RomError("압축 .code의 해제 크기가 잘못되었습니다.")

    footer = (top_bottom >> 24) & 0xFF
    encoded = top_bottom & 0xFFFFFF
    if footer < 8 or footer > len(data) or encoded < footer or encoded > len(data):
        raise RomError("압축 .code footer가 잘못되었습니다.")

    idx = len(data) - footer
    stop = len(data) - encoded
    out_idx = out_size
    out = bytearray(out_size)
    out[:len(data)] = data

    while idx > stop:
        idx -= 1
        control = data[idx]
        for _ in range(8):
            if idx <= stop or out_idx == 0:
                break
            if control & 0x80:
                if idx < 2:
                    raise RomError("압축 .code back-reference가 잘렸습니다.")
                idx -= 2
                seg = data[idx] | (data[idx + 1] << 8)
                seg_size = ((seg >> 12) & 0xF) + 3
                seg_off = (seg & 0xFFF) + 2
                if out_idx < seg_size:
                    raise RomError("압축 .code 출력 범위 오류")
                for _ in range(seg_size):
                    src = out_idx + seg_off
                    if src >= len(out):
                        raise RomError("압축 .code 참조 범위 오류")
                    out_idx -= 1
                    out[out_idx] = out[src]
            else:
                if idx <= stop or out_idx == 0:
                    raise RomError("압축 .code literal이 잘렸습니다.")
                idx -= 1
                out_idx -= 1
                out[out_idx] = data[idx]
            control = (control << 1) & 0xFF
    return bytes(out)


def write_bytes(path: Path, data: bytes) -> Extracted:
    path.write_bytes(data)
    return Extracted(path.resolve(), len(data), hashlib.sha256(data).hexdigest())


def copy_region(f, off: int, size: int, dest: Path) -> Extracted:
    h = hashlib.sha256()
    left = size
    f.seek(off)
    with dest.open("wb") as out:
        while left:
            b = f.read(min(left, COPY_CHUNK))
            if not b:
                raise RomError("RomFS 추출 중 ROM이 끝났습니다.")
            out.write(b)
            h.update(b)
            left -= len(b)
    return Extracted(dest.resolve(), size, h.hexdigest())


def extract_rom(rom: Path, out_dir: Path, log) -> tuple[Layout, dict[str, Extracted]]:
    layout = find_layout(rom)
    if layout.program_id != KOR_TITLE_ID:
        raise RomError(
            f"한국판 시간의 오카리나 3D가 아닙니다.\n"
            f"필요 Title ID: {KOR_TITLE_ID_TEXT}\n"
            f"감지 Title ID: {layout.program_id:016X}\n"
            f"Product Code: {layout.product_code or '(없음)'}"
        )

    required = layout.romfs_size + layout.code_size + 64 * 1024 * 1024
    free = shutil.disk_usage(out_dir.parent).free
    if free < required:
        raise RomError(
            f"저장 공간이 부족합니다. 최소 약 {required // (1024*1024)} MiB가 필요합니다."
        )

    out_dir.mkdir(parents=True, exist_ok=False)
    log(f"Title ID 확인: {layout.program_id:016X}")
    log(f"Product Code: {layout.product_code}")

    with rom.open("rb") as f:
        exheader_data = read_exact(f, layout.exheader_offset, 0x800, "ExHeader")
        exheader = write_bytes(out_dir / "exheader.bin", exheader_data)

        log("code.bin 추출 중...")
        code_raw = read_exact(f, layout.code_offset, layout.code_size, ".code")
        code_data = decompress_code(code_raw) if layout.compressed_code else code_raw
        code = write_bytes(out_dir / "code.bin", code_data)

        log("romfs.bin 추출 중...")
        romfs = copy_region(f, layout.romfs_offset, layout.romfs_size, out_dir / "romfs.bin")

    return layout, {"code": code, "exheader": exheader, "romfs": romfs}


def process_contract(exheader_path: Path) -> dict:
    h = exheader_path.read_bytes()
    if len(h) != 0x800:
        raise RomError("ExHeader 크기가 0x800이 아닙니다.")
    entry = u32(h, 0x10)
    if entry == 0 or entry & 0xFFF:
        raise RomError(f"ExHeader의 코드 시작 주소가 이상합니다: 0x{entry:08X}")
    return {"entrypoint": entry}


def create_pack(rom: Path, target: Path, triaevum_dir: Path | None, log) -> Path:
    target = target.resolve()
    target.mkdir(parents=True, exist_ok=True)
    session = target / ("TriAevum-KOR-" + time.strftime("%Y%m%d-%H%M%S"))
    n = 1
    while session.exists():
        session = target / ("TriAevum-KOR-" + time.strftime("%Y%m%d-%H%M%S") + f"-{n}")
        n += 1
    session.mkdir()

    extracted_dir = session / "inputs"
    layout, files = extract_rom(rom, extracted_dir, log)
    process = process_contract(files["exheader"].path)

    recipe_id = "oot3d-kor-local-" + files["code"].sha256[:8]
    recipe = {
        "id": recipe_id,
        "title": "The Legend of Zelda: Ocarina of Time 3D",
        "region": "KOR local private input",
        "title_id": KOR_TITLE_ID_TEXT,
        "product_code": layout.product_code,
        "inputs": {
            k: {"bytes": v.bytes, "sha256": v.sha256}
            for k, v in files.items()
        },
        "process": process,
        "dialogue_languages": ["Korean"],
        "qualification": "local_user_rom_unqualified",
    }
    recipe_doc = {"format": "triaevum_supported_revisions_v1", "recipes": [recipe]}
    recipe_path = session / "oot3d-kor-local.json"
    recipe_path.write_text(json.dumps(recipe_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    texture = session / "mods" / "load" / "textures" / KOR_TITLE_ID_TEXT
    dump = session / "mods" / "dump" / "textures" / KOR_TITLE_ID_TEXT
    code_mods = session / "mods" / "code-patches-KOR-only"
    texture.mkdir(parents=True)
    dump.mkdir(parents=True)
    code_mods.mkdir(parents=True)

    installed = None
    if triaevum_dir:
        triaevum_dir = triaevum_dir.resolve()
        data = triaevum_dir / "data"
        installed_texture = data / "load" / "textures" / KOR_TITLE_ID_TEXT
        installed_dump = data / "dump" / "textures" / KOR_TITLE_ID_TEXT
        installed_kor = data / "kor"
        installed_texture.mkdir(parents=True, exist_ok=True)
        installed_dump.mkdir(parents=True, exist_ok=True)
        installed_kor.mkdir(parents=True, exist_ok=True)
        shutil.copy2(recipe_path, installed_kor / "oot3d-kor-local.json")
        installed = {
            "triaevum_dir": str(triaevum_dir),
            "texture_dir": str(installed_texture),
            "dump_dir": str(installed_dump),
            "recipe_copy": str(installed_kor / "oot3d-kor-local.json"),
        }
        log("선택한 TriAevum 폴더에도 KOR 데이터/모드 폴더를 준비했습니다.")

    report = {
        "format": "triaevum_kor_fresh_report_v1",
        "title_id": KOR_TITLE_ID_TEXT,
        "product_code": layout.product_code,
        "container_kind": layout.container_kind,
        "partition_index": layout.partition_index,
        "recipe": recipe_id,
        "inputs": {
            k: {"path": str(v.path), "bytes": v.bytes, "sha256": v.sha256}
            for k, v in files.items()
        },
        "mod_paths": {
            "custom_textures": str(texture),
            "texture_dump": str(dump),
            "kor_code_patches": str(code_mods),
        },
        "installed": installed,
        "limits": [
            "현재 공개 TriAevum precompiled 타이틀 모듈은 KOR용으로 검증되어 있지 않습니다.",
            "텍스처 모드는 KOR Title ID 폴더를 사용할 수 있습니다.",
            "IPS/메모리 주소/실행 코드 패치는 한국판 주소에 맞춘 별도 포팅이 필요합니다.",
            "이 도구는 ROM을 수정하거나 배포하지 않습니다.",
        ],
    }
    (session / "KOR_REPORT.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    (session / "README_KOR.txt").write_text(
        "TriAevum KOR Fresh\n"
        "==================\n\n"
        f"Title ID: {KOR_TITLE_ID_TEXT}\n"
        f"Product Code: {layout.product_code}\n"
        f"Recipe: {recipe_path}\n\n"
        "이 폴더의 inputs에는 사용자가 선택한 ROM에서 로컬로 추출한 code.bin, exheader.bin, romfs.bin이 있습니다.\n"
        "이 파일들은 게임 원본 데이터이므로 다른 사람에게 배포하지 마세요.\n\n"
        "모드:\n"
        f"- 커스텀 텍스처: {texture}\n"
        f"- 텍스처 덤프: {dump}\n"
        f"- 한국판 전용 코드패치 보관: {code_mods}\n\n"
        "주의: 현재 공개 TriAevum의 precompiled 실행 모듈은 KOR판을 공식 지원하지 않습니다.\n"
        "이 도구는 KOR ROM 식별/추출/레시피/모드 경로 준비를 정확히 수행하지만,\n"
        "EUR/USA용 실행 코드 모드를 한국판으로 자동 변환하지는 않습니다.\n",
        encoding="utf-8",
    )

    log("KOR 패키지 생성 완료.")
    return session


def log_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TriAevumKOR"
    base.mkdir(parents=True, exist_ok=True)
    return base / "TriAevumKOR-error.log"


def write_error_log(text: str):
    try:
        log_path().write_text(text, encoding="utf-8")
    except Exception:
        pass


def build_synthetic_rom(path: Path):
    size = 0x4400
    b = bytearray(size)
    b[0x100:0x104] = b"NCCH"
    struct.pack_into("<Q", b, 0x118, KOR_TITLE_ID)
    b[0x150:0x160] = KOR_PRODUCT_CODE.encode("ascii").ljust(16, b"\0")
    struct.pack_into("<I", b, 0x180, 0x400)
    b[0x18D] = 0x02
    struct.pack_into("<I", b, 0x1A0, 0x10)
    struct.pack_into("<I", b, 0x1A4, 0x04)
    struct.pack_into("<I", b, 0x1B0, 0x20)
    struct.pack_into("<I", b, 0x1B4, 0x02)

    ex = 0x200
    struct.pack_into("<I", b, ex + 0x10, 0x00100000)
    b[ex + 0x0D] = 0

    exefs = 0x2000
    b[exefs:exefs+8] = b".code\0\0\0"
    struct.pack_into("<I", b, exefs + 8, 0)
    struct.pack_into("<I", b, exefs + 12, 0x200)
    for i in range(0x200):
        b[exefs + 0x200 + i] = i & 0xFF

    romfs = 0x4000
    b[romfs:romfs+4] = b"IVFC"
    path.write_bytes(b)


def self_test() -> int:
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rom = root / "test.3ds"
            build_synthetic_rom(rom)
            layout = find_layout(rom)
            assert layout.program_id == KOR_TITLE_ID
            assert layout.product_code == KOR_PRODUCT_CODE
            session = create_pack(rom, root / "out", None, lambda *_: None)
            report = json.loads((session / "KOR_REPORT.json").read_text(encoding="utf-8"))
            assert report["title_id"] == KOR_TITLE_ID_TEXT
            assert (session / "inputs" / "code.bin").stat().st_size == 0x200
            assert (session / "inputs" / "romfs.bin").stat().st_size == 0x400
        return 0
    except Exception:
        write_error_log(traceback.format_exc())
        return 1


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("780x610")
        self.minsize(700, 540)
        self.rom_var = tk.StringVar()
        self.out_var = tk.StringVar(value=str(Path.home() / "TriAevumKOR"))
        self.triaevum_var = tk.StringVar()
        self.status_var = tk.StringVar(value="준비됨")
        self.last_session: Path | None = None

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="TriAevum 한국판 ROM 준비 도구 — 새로 제작한 독립형 버전",
                  font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "복호화된 한국판 시간의 오카리나 3D ROM을 직접 읽어 KOR 레시피와 모드 폴더를 만듭니다. "
                "TriAevum Python 모듈을 EXE 내부에서 불러오지 않기 때문에 이전 버전의 내부 경로 오류가 없습니다."
            ),
            wraplength=730
        ).pack(anchor="w", pady=(6, 14))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        form.columnconfigure(0, weight=1)

        ttk.Label(form, text="1. 한국판 복호화 ROM (.3ds/.cci)").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.rom_var).grid(row=1, column=0, sticky="ew", padx=(0,8), pady=(3,10))
        ttk.Button(form, text="ROM 선택", command=self.pick_rom).grid(row=1, column=1, pady=(3,10))

        ttk.Label(form, text="2. 결과 저장 폴더").grid(row=2, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.out_var).grid(row=3, column=0, sticky="ew", padx=(0,8), pady=(3,10))
        ttk.Button(form, text="폴더 선택", command=self.pick_out).grid(row=3, column=1, pady=(3,10))

        ttk.Label(form, text="3. TriAevum 설치 폴더 (선택 사항)").grid(row=4, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.triaevum_var).grid(row=5, column=0, sticky="ew", padx=(0,8), pady=(3,12))
        ttk.Button(form, text="폴더 선택", command=self.pick_triaevum).grid(row=5, column=1, pady=(3,12))

        self.run_btn = ttk.Button(outer, text="한국판 호환 준비 파일 만들기", command=self.start)
        self.run_btn.pack(fill="x", pady=(2,8))
        self.open_btn = ttk.Button(outer, text="결과 폴더 열기", command=self.open_result, state="disabled")
        self.open_btn.pack(fill="x", pady=(0,10))

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x")
        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w", pady=(6,5))

        self.logbox = tk.Text(outer, height=15, wrap="word", state="disabled")
        self.logbox.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text=(
                "지원: KOR ROM 식별/추출, KOR 레시피, KOR Title ID 커스텀 텍스처 경로. "
                "제한: EUR/USA용 IPS·메모리 주소 기반 코드 모드는 자동 변환하지 않습니다."
            ),
            wraplength=730
        ).pack(anchor="w", pady=(10,0))

    def pick_rom(self):
        p = filedialog.askopenfilename(
            title="복호화된 한국판 ROM 선택",
            filetypes=[("Nintendo 3DS ROM", "*.3ds *.cci"), ("All files", "*.*")]
        )
        if p:
            self.rom_var.set(p)

    def pick_out(self):
        p = filedialog.askdirectory(title="결과 저장 폴더")
        if p:
            self.out_var.set(p)

    def pick_triaevum(self):
        p = filedialog.askdirectory(title="TriAevum 설치 폴더 (선택)")
        if p:
            self.triaevum_var.set(p)

    def log(self, s: str):
        def add():
            self.logbox.configure(state="normal")
            self.logbox.insert("end", s.rstrip() + "\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
        self.after(0, add)

    def start(self):
        if not self.rom_var.get().strip():
            messagebox.showerror(APP_NAME, "ROM을 선택하세요.")
            return
        if not self.out_var.get().strip():
            messagebox.showerror(APP_NAME, "결과 저장 폴더를 선택하세요.")
            return

        rom = Path(self.rom_var.get().strip())
        out = Path(self.out_var.get().strip())
        tri = Path(self.triaevum_var.get().strip()) if self.triaevum_var.get().strip() else None

        self.run_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.progress.start(12)
        self.status_var.set("작업 중...")
        self.log("처음부터 새 방식으로 작업을 시작합니다.")
        self.log("원본 ROM은 수정하지 않습니다.")

        def worker():
            try:
                session = create_pack(rom, out, tri, self.log)
                self.last_session = session
                self.after(0, self.ok)
            except Exception as exc:
                detail = traceback.format_exc()
                self.log("")
                self.log("오류: " + str(exc))
                self.log(detail)
                write_error_log(self.logbox.get("1.0", "end") + "\n" + detail)
                self.after(0, lambda: self.fail(str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def ok(self):
        self.progress.stop()
        self.status_var.set("완료")
        self.run_btn.configure(state="normal")
        self.open_btn.configure(state="normal")
        messagebox.showinfo(APP_NAME, "한국판 KOR 준비 파일 생성을 완료했습니다.")

    def fail(self, msg: str):
        self.progress.stop()
        self.status_var.set("오류")
        self.run_btn.configure(state="normal")
        messagebox.showerror(APP_NAME, msg + f"\n\n오류 로그: {log_path()}")

    def open_result(self):
        if self.last_session:
            try:
                os.startfile(self.last_session)  # type: ignore[attr-defined]
            except Exception:
                messagebox.showinfo(APP_NAME, str(self.last_session))


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--self-test", action="store_true")
    ns, _ = parser.parse_known_args()
    if ns.self_test:
        return self_test()
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# fresh-build-trigger

# retest-trigger

# final-selftest-trigger
