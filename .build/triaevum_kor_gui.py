from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import capstone  # bundled dependency used by TriAevum tooling
    import certifi   # bundled dependency used by TriAevum tooling
except Exception:
    capstone = None
    certifi = None

APP_TITLE = "TriAevum KOR Helper"
KOR_TITLE_ID = 0x000400000008F800
KOR_TITLE_ID_TEXT = "000400000008F800"
KOR_PRODUCT_CODE = "CTR-P-AQEK"
UPSTREAM_COMMIT = "a9b447709d4405848d75352354891059cebb9ff8"


def bundled_source_root() -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS"))
    else:
        base = Path(__file__).resolve().parent
    return base / "triaevum_src"


def default_data_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "TriAevumKOR" / "data"
    return Path.home() / "TriAevumKOR" / "data"


def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def load_triaevum():
    root = bundled_source_root()
    marker = root / "tools" / "triaevum_release" / "ctr_rom.py"
    if not marker.is_file():
        raise RuntimeError(
            "Bundled TriAevum source is missing. Re-download this helper build."
        )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    importlib.invalidate_caches()
    from tools.triaevum_release import forge
    from tools.triaevum_release.ctr_rom import CtrRomError, extract_decrypted_rom
    return root, forge, CtrRomError, extract_decrypted_rom


def exact_input(extracted_file) -> dict:
    return {
        "bytes": int(extracted_file.bytes),
        "sha256": str(extracted_file.sha256).lower(),
    }


def make_recipe(extracted, manifest: dict) -> dict:
    process = manifest.get("process")
    if not isinstance(process, dict):
        raise RuntimeError("TriAevum could not derive the CTR process manifest.")

    process_contract = {}
    for key in ("base", "entrypoint", "executable_size"):
        value = process.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            process_contract[key] = value

    if "entrypoint" not in process_contract:
        raise RuntimeError("The Korean ROM process entrypoint could not be determined.")

    return {
        "id": f"oot3d-kor-local-{extracted.code.sha256[:8]}",
        "title": "The Legend of Zelda: Ocarina of Time 3D",
        "region": "KOR local private input",
        "product_code": KOR_PRODUCT_CODE,
        "title_id": KOR_TITLE_ID_TEXT,
        "inputs": {
            "code": exact_input(extracted.code),
            "exheader": exact_input(extracted.exheader),
            "romfs": exact_input(extracted.romfs),
        },
        "process": process_contract,
        "dialogue_languages": ["Korean"],
        "qualification": "local_user_rom_unqualified",
    }


def prepare_kor_rom(rom: Path, data_root: Path, log):
    rom = rom.expanduser().resolve()
    data_root = data_root.expanduser().resolve()
    data_root.mkdir(parents=True, exist_ok=True)

    if not rom.is_file():
        raise RuntimeError("ROM file does not exist.")
    if rom.suffix.lower() not in {".3ds", ".cci"}:
        raise RuntimeError("Select a decrypted .3ds or .cci file.")

    source_root, forge, CtrRomError, extract_decrypted_rom = load_triaevum()

    work_root = data_root / "kor-helper"
    work_root.mkdir(parents=True, exist_ok=True)
    session = work_root / time.strftime("%Y%m%d-%H%M%S")
    counter = 1
    base_session = session
    while session.exists():
        session = Path(str(base_session) + f"-{counter}")
        counter += 1
    extracted_dir = session / "extracted"
    session.mkdir(parents=True, exist_ok=False)

    log("ROM을 읽는 중입니다. 원본 ROM은 수정하지 않습니다.")
    try:
        extracted = extract_decrypted_rom(
            rom,
            extracted_dir,
            require_rom_suffix=True,
        )
    except CtrRomError as exc:
        raise RuntimeError(
            "ROM 추출에 실패했습니다. TriAevum은 이미 복호화된 .3ds/.cci만 "
            f"지원합니다.\n\n{exc}"
        ) from exc

    log(f"감지된 Title ID: {extracted.program_id:016X}")
    if extracted.program_id != KOR_TITLE_ID:
        raise RuntimeError(
            "한국판 시간의 오카리나 3D ROM이 아닙니다.\n"
            f"필요한 Title ID: {KOR_TITLE_ID_TEXT}\n"
            f"감지된 Title ID: {extracted.program_id:016X}"
        )

    log("code / ExHeader / RomFS 해시를 계산했습니다.")
    manifest = forge._build_process_manifest(
        extracted.exheader.path,
        extracted.code.path,
        extracted.romfs.path,
    )
    recipe = make_recipe(extracted, manifest)

    recipe_doc = {
        "format": "triaevum_supported_revisions_v1",
        "recipes": [recipe],
    }
    recipe_path = session / "oot3d-kor-local.json"
    recipe_path.write_text(
        json.dumps(recipe_doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    log("한국판 전용 로컬 recipe를 생성했습니다.")
    cache = forge.HashCache(session / ".hash-cache.json", enabled=True)
    verified, _ = forge.verify_sources(
        recipe,
        code_path=extracted.code.path,
        exheader_path=extracted.exheader.path,
        romfs_path=extracted.romfs.path,
        cache=cache,
    )

    log("TriAevum private content index를 준비합니다.")
    prepared = forge.prepare_content(
        recipe,
        verified,
        output_root=data_root / "titles",
    )
    prepared_dir = Path(prepared["directory"]).resolve()

    texture_dir = data_root / "load" / "textures" / KOR_TITLE_ID_TEXT
    dump_dir = data_root / "dump" / "textures" / KOR_TITLE_ID_TEXT
    texture_dir.mkdir(parents=True, exist_ok=True)
    dump_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "format": "triaevum_kor_helper_report_v1",
        "upstream_commit": UPSTREAM_COMMIT,
        "product_code": KOR_PRODUCT_CODE,
        "title_id": KOR_TITLE_ID_TEXT,
        "source_rom": {
            "name": rom.name,
            "bytes": rom.stat().st_size,
            "sha256": sha256_file(rom),
        },
        "recipe": recipe,
        "prepared_directory": str(prepared_dir),
        "custom_texture_directory": str(texture_dir),
        "texture_dump_directory": str(dump_dir),
        "notes": [
            "No ROM or original game asset is redistributed by this helper.",
            "Azahar-compatible custom textures can be placed in the custom texture directory.",
            "Executable/code patch mods (IPS, address patches, cheats) are NOT automatically region-portable.",
            "The official public TriAevum package does not currently ship a precompiled KOR title module.",
        ],
    }
    report_path = session / "KOR_REPORT.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    readme = session / "README_KOR.txt"
    readme.write_text(
        "TriAevum KOR Helper 결과\n"
        "========================\n\n"
        f"Title ID: {KOR_TITLE_ID_TEXT}\n"
        f"Product Code: {KOR_PRODUCT_CODE}\n"
        f"Recipe: {recipe_path}\n"
        f"Prepared data: {prepared_dir}\n"
        f"커스텀 텍스처 폴더: {texture_dir}\n"
        f"텍스처 덤프 폴더: {dump_dir}\n\n"
        "지원 범위\n"
        "- 한국판 ROM의 code / ExHeader / RomFS 정확한 식별값 생성\n"
        "- TriAevum private content index 준비\n"
        "- 한국판 Title ID용 Azahar 호환 커스텀 텍스처 폴더 생성\n\n"
        "주의\n"
        "- 이 도구는 ROM을 수정하거나 포함하지 않습니다.\n"
        "- IPS/메모리 주소 기반 코드 모드는 지역별 주소가 다를 수 있어 자동 호환되지 않습니다.\n"
        "- 현재 공개 TriAevum에는 KOR용 precompiled title DLL이 없으므로, 실제 KOR 실행 모듈은 "
        "TriAevum 개발용 build-title 경로로 별도 빌드/검증이 필요합니다.\n",
        encoding="utf-8",
    )

    log("완료했습니다.")
    return {
        "session": session,
        "recipe": recipe_path,
        "report": report_path,
        "prepared": prepared_dir,
        "texture": texture_dir,
        "dump": dump_dir,
    }


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("760x560")
        self.minsize(680, 500)

        self.rom_var = tk.StringVar()
        self.data_var = tk.StringVar(value=str(default_data_root()))
        self.status_var = tk.StringVar(value="준비됨")
        self.last_result = None

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="TriAevum 한국판 ROM 호환 준비 도구",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "한국판 시간의 오카리나 3D 복호화 ROM을 분석해 로컬 KOR recipe와 "
                "모드용 텍스처 폴더를 만듭니다. ROM은 수정하거나 업로드하지 않습니다."
            ),
            wraplength=700,
        ).pack(anchor="w", pady=(6, 16))

        form = ttk.Frame(outer)
        form.pack(fill="x")

        ttk.Label(form, text="한국판 ROM (.3ds/.cci)").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.rom_var).grid(
            row=1, column=0, sticky="ew", padx=(0, 8), pady=(3, 12)
        )
        ttk.Button(form, text="찾아보기", command=self.browse_rom).grid(
            row=1, column=1, sticky="ew", pady=(3, 12)
        )

        ttk.Label(form, text="작업/데이터 폴더").grid(row=2, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.data_var).grid(
            row=3, column=0, sticky="ew", padx=(0, 8), pady=(3, 12)
        )
        ttk.Button(form, text="찾아보기", command=self.browse_data).grid(
            row=3, column=1, sticky="ew", pady=(3, 12)
        )
        form.columnconfigure(0, weight=1)

        self.run_button = ttk.Button(
            outer,
            text="한국판 호환 준비 파일 만들기",
            command=self.start_prepare,
        )
        self.run_button.pack(fill="x", pady=(4, 8))

        self.open_button = ttk.Button(
            outer,
            text="완료 폴더 열기",
            command=self.open_result,
            state="disabled",
        )
        self.open_button.pack(fill="x", pady=(0, 14))

        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x")

        ttk.Label(outer, textvariable=self.status_var).pack(anchor="w", pady=(6, 6))

        self.log_box = tk.Text(outer, height=15, wrap="word", state="disabled")
        self.log_box.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text=(
                "중요: 커스텀 텍스처는 KOR Title ID 폴더로 준비하지만, IPS/주소 기반 코드 모드는 "
                "한국판에 맞춘 별도 포팅이 필요합니다."
            ),
            wraplength=700,
        ).pack(anchor="w", pady=(10, 0))

    def browse_rom(self):
        path = filedialog.askopenfilename(
            title="한국판 복호화 ROM 선택",
            filetypes=[
                ("Nintendo 3DS ROM", "*.3ds *.cci"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.rom_var.set(path)

    def browse_data(self):
        path = filedialog.askdirectory(title="작업/데이터 폴더 선택")
        if path:
            self.data_var.set(path)

    def log(self, message: str):
        def append():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", message.rstrip() + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, append)

    def start_prepare(self):
        rom = Path(self.rom_var.get().strip())
        data = Path(self.data_var.get().strip())
        if not self.rom_var.get().strip():
            messagebox.showerror(APP_TITLE, "ROM 파일을 선택하세요.")
            return
        if not self.data_var.get().strip():
            messagebox.showerror(APP_TITLE, "작업/데이터 폴더를 선택하세요.")
            return

        self.run_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.progress.start(12)
        self.status_var.set("작업 중...")
        self.log("작업을 시작합니다.")

        def worker():
            try:
                result = prepare_kor_rom(rom, data, self.log)
            except Exception as exc:
                self.log("")
                self.log("오류:")
                self.log(str(exc))
                self.log(traceback.format_exc())
                self.after(0, lambda: self.finish_error(str(exc)))
                return
            self.last_result = result
            self.after(0, self.finish_ok)

        threading.Thread(target=worker, daemon=True).start()

    def finish_ok(self):
        self.progress.stop()
        self.status_var.set("완료")
        self.run_button.configure(state="normal")
        self.open_button.configure(state="normal")
        messagebox.showinfo(
            APP_TITLE,
            "한국판 ROM 분석과 KOR 준비 파일 생성을 완료했습니다.\n\n"
            "완료 폴더에서 recipe/report와 모드 폴더 경로를 확인하세요.",
        )

    def finish_error(self, message: str):
        self.progress.stop()
        self.status_var.set("오류")
        self.run_button.configure(state="normal")
        messagebox.showerror(APP_TITLE, message)

    def open_result(self):
        if not self.last_result:
            return
        path = Path(self.last_result["session"])
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception:
            messagebox.showinfo(APP_TITLE, str(path))


if __name__ == "__main__":
    App().mainloop()

# build-trigger
