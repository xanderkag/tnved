"""
Строит FAISS-индекс по описаниям кодов ТН ВЭД.

Запуск: python build_index.py
Результат:
  data/tnved_vecs.npy        — промежуточные векторы (переиспользуются)
  data/tnved.faiss           — векторный индекс
  data/tnved_meta.json       — список {code, description, full_path, ...}
  data/tnved_index_info.json — каким бэкендом и в какой размерности собран

Текст вектора — путь кода из базы с названиями позиций у ссылок номером
(build_index_texts); тот же текст пишется в tnved_meta.json как index_text.

Векторизацией занимается embedder.py — bge-m3 на GPU-сервере через
`/v1/embeddings` (EMBEDDINGS_BASE_URL; быстро, ничего локально не нужно), либо,
только явно, локальная e5 на CPU (EMBEDDER_BACKEND=local, requirements-e5.txt).
Подробнее — docstring embedder.py. Индекс собирается здесь, заранее: при сборке
Docker-образа он не строится, образ берёт готовые файлы из data/.

Прогон разведён на два этапа, каждый в своём процессе:
  embed — считает векторы, пишет .npy
  index — читает .npy, собирает FAISS
Причина разделения: локальный бэкенд тянет torch, и torch с faiss в одном
процессе на некоторых сборках роняют интерпретатор. Плюс так этап embed
возобновляем: считается кусками, каждый кусок сразу на диск.
Без аргументов скрипт запускает оба этапа сам, по очереди.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "tnved.db"
VECS_PATH = BASE / "data" / "tnved_vecs.npy"
PARTS_DIR = BASE / "data" / "vecs_parts"
FAISS_PATH = BASE / "data" / "tnved.faiss"
META_PATH = BASE / "data" / "tnved_meta.json"
INFO_PATH = BASE / "data" / "tnved_index_info.json"
VECS_INFO_PATH = BASE / "data" / "tnved_vecs_info.json"
CHUNK_SIZE = int(os.environ.get("EMBED_CHUNK", "1024"))
# Актуально только для локального бэкенда: на слабом CPU многопоточность
# torch плюс нехватка памяти давали access violation.
EMBED_THREADS = int(os.environ.get("EMBED_THREADS", "8"))

PATH_SEP = " → "
# Группа одна на все свои коды: внутри группы она не различает, а длинная только
# размывает вектор. Берём её до первой «;» и не длиннее этого.
GROUP_TEXT_LIMIT = 140
# Ссылка на позицию номером: «машин товарной позиции 8471», «товарных позиций
# 8470 - 8472», «товарной позиции 5903, 5906 или 5907», «подсубпозиции 8472 90 300 0».
_CODE = r"\d{4}(?:\s?\d{2}(?:\s?\d{2,3}(?:\s?\d)?)?)?"
REF_RE = re.compile(rf"позици[а-яё]*\s*({_CODE}(?:\s*(?:[-–—]|,|или|и)\s*{_CODE})*)", re.I)
# Ссылка в исключении — «жир …, кроме жира товарной позиции 1503»: туда название
# не ставим, иначе запрос про жир 1503 притянет код, который его как раз исключает.
NEG_RE = re.compile(r"кроме|исключени|исключая|не включ|отличн", re.I)
GENERIC = {"прочие", "прочая", "прочий", "прочее", "другие"}
REF_RANGE_MAX = 3  # «8470 - 8472» раскрываем, «3901 - 3914» — нет: 14 названий не помогут
REF_NAMES_MAX = 3


def _part_path(offset: int, count: int) -> Path:
    return PARTS_DIR / f"part_{offset:06d}_{count}.npy"


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("ё", "е").split())


def _case(seg: str) -> str:
    """Уровни дерева 2017 года записаны заглавными — к виду тарифа: «Вычислительные машины…»."""
    return seg[:1] + seg[1:].lower() if seg.isupper() else seg


def _short_name(desc: str) -> str:
    """«ВЫЧИСЛИТЕЛЬНЫЕ МАШИНЫ И ИХ БЛОКИ; МАГНИТНЫЕ …» → «вычислительные машины и их блоки»."""
    text, prev = " ".join(desc.split()), None
    while text != prev:  # «(например, …)», «(кроме …)» — изнутри наружу
        prev, text = text, re.sub(r"\s*\([^()]*\)", "", text)
    # незакрытая скобка — наименование обрезано в дереве посреди «(например, …»
    text = re.split(r"[;(]", text)[0].strip(" ,.:")
    if len(text) > 120:
        cut = text[:120]
        text = cut.rsplit(",", 1)[0] if "," in cut[40:] else cut.rsplit(" ", 1)[0]
    return text.lower()


def _near_dup(a: str, b: str) -> bool:
    """Один уровень дважды подряд: позиция из дерева 2017 и она же из цепочки тарифа
    («…для машин товарных позиций 8469 - 8472» и «…8470 - 8472») или та же строка,
    обрезанная в дереве."""
    na, nb = _norm(a), _norm(b)
    k = 0
    while k < min(len(na), len(nb)) and na[k] == nb[k]:
        k += 1
    return na == nb or k >= 40


def _ref_codes(nums: str) -> list[str] | None:
    """«8470 - 8472» → 8470, 8471, 8472; «5903, 5906 или 5907» → три кода.

    None — ссылка слишком широкая, чтобы её называть: диапазон больше REF_RANGE_MAX
    («3901 - 3914») или больше REF_NAMES_MAX позиций («8601 - 8606, 8701 - 8705, …, 8806»).
    Три случайных названия из такого списка не поясняют, а сбивают."""
    tokens = re.findall(rf"{_CODE}|[-–—]", nums)
    codes: list[str] = []
    i = 0
    while i < len(tokens):
        start = re.sub(r"\D", "", tokens[i])
        if i + 2 < len(tokens) and tokens[i + 1] in ("-", "–", "—"):
            end = re.sub(r"\D", "", tokens[i + 2])
            if not (len(start) == len(end) == 4 and 0 < int(end) - int(start) <= REF_RANGE_MAX):
                return None
            codes += [str(n).zfill(4) for n in range(int(start), int(end) + 1)]
            i += 3
            continue
        codes.append(start)
        i += 1
    return codes if len({c[:4] for c in codes}) <= REF_NAMES_MAX else None


def _with_ref_names(seg: str, headings: dict[str, str], desc: dict[str, str]) -> str:
    """«части и принадлежности машин товарной позиции 8471» →
    «… 8471 (вычислительные машины и их блоки)». Без названия в тексте 8473 30 нет
    ни «вычислительных машин», ни «сервера», и деталь сервера его не находит."""
    out: list[str] = []
    pos = 0
    for m in REF_RE.finditer(seg):
        clause, prev = seg[:m.start()].rsplit(";", 1)[-1], None
        while clause != prev:  # «(кроме футляров, чехлов …), предназначенные для» — не исключение
            prev, clause = clause, re.sub(r"\([^()]*\)", "", clause)
        if NEG_RE.search(clause):
            continue
        names: list[str] = []
        for code in _ref_codes(m.group(1)) or []:
            name = headings.get(code[:4])
            if not name:
                continue
            own = _short_name(desc.get(code) or "") if len(code) > 4 else ""
            if own and own not in GENERIC and not re.search(r"\d{4}", own):
                name = f"{name}: {own}"
            if name not in names:
                names.append(name)
        if names:
            out.append(seg[pos:m.end()] + " (" + "; ".join(names[:REF_NAMES_MAX]) + ")")
            pos = m.end()
    out.append(seg[pos:])
    return "".join(out)


def build_index_texts(rows: list[dict]) -> list[str]:
    """Текст для вектора — путь кода из базы (parse_tnved.py): группа → позиция → … → код.

    В пути у кодов тарифа ниже позиции — цепочка самого тарифа в действующей редакции,
    без пометок «(с 01.01.2022)» и сносок; выше — узлы дерева. Поверх пути:
      - ссылка на позицию номером получает название позиции (кроме ссылок в исключениях);
      - уровни дерева 2017 года — строчными, как в тарифе;
      - позиция, записанная дважды (в дереве и в тарифе), остаётся одна — вторая;
      - группа — до первой «;». Остальные уровни не режем: позиция 8473 обрезалась
        на «…предназначенные исключительно или в основном для…», и терялось, для чего.

    Замер 25.09.2026 против текстов 30.07 (bge-m3, место ожидаемого кода среди
    действующих кодов группы): 30 синтетических описаний — в первых 12 было 24, стало 28,
    MRR 0,62 → 0,78; «Части вычислительной машины (сервера)» — 8473 30 с 7-го места на 1-е.
    Английские описания из инвойсов почти не выиграли — это не текст индекса, а язык запроса.
    """
    with sqlite3.connect(DB_PATH) as conn:
        desc = dict(conn.execute("SELECT code, description FROM codes").fetchall())
    headings = {code: _short_name(d) for code, d in desc.items() if len(code) == 4 and d}

    texts = []
    named = 0
    for row in rows:
        path = (row["full_path"] or row["description"] or "").strip()
        segs = [_case(s.strip()) for s in path.split(PATH_SEP) if s.strip()]
        segs = [s for s, nxt in zip(segs, segs[1:] + [""]) if not (nxt and _near_dup(s, nxt))]
        if len(segs) > 1:
            group = segs[0].split(";")[0].strip(" ,.:")
            if len(group) > GROUP_TEXT_LIMIT:
                group = group[:GROUP_TEXT_LIMIT].rsplit(" ", 1)[0]
            segs[0] = group
        text = PATH_SEP.join(segs)
        texts.append(PATH_SEP.join(_with_ref_names(s, headings, desc) for s in segs))
        named += texts[-1] != text
    print(f"Текстов: {len(texts):,}; с названием позиции у ссылки номером — {named:,}")
    return texts


def load_leaves() -> list[dict]:
    if not DB_PATH.exists():
        print(f"База данных не найдена: {DB_PATH}")
        print("Запустите: python parse_tnved.py")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # Берём все коды, не только level=4, чтобы не зависеть от качества данных
    cur.execute("""
        SELECT code, description, full_path, duty_rate, data_source
        FROM codes
        WHERE level >= 3
        ORDER BY code
    """)
    rows = [
        {
            "code": r[0],
            "description": r[1],
            "full_path": r[2],
            "duty_rate": r[3],
            "data_source": r[4],
        }
        for r in cur.fetchall()
    ]
    conn.close()
    return rows


def stage_embed() -> None:
    """Этап 1: тексты → векторы в data/tnved_vecs.npy. faiss здесь не импортируем.

    Считаем кусками по CHUNK_SIZE и сохраняем каждый кусок отдельным .npy,
    чтобы обрыв на середине не обнулял работу: повторный запуск досчитывает
    только недостающие куски. На локальном CPU это 3–6 текстов/с (часы),
    через bge-m3 на GPU — минуты.
    """
    import numpy as np

    from embedder import active_backend, expected_name, get_embedder

    backend = active_backend()
    if backend == "local" and EMBED_THREADS:
        try:
            import torch
        except ImportError:
            sys.exit("EMBEDDER_BACKEND=local: нужен sentence-transformers — "
                     "pip install -r requirements-e5.txt")

        torch.set_num_threads(EMBED_THREADS)

    print(f"Загружаем коды из {DB_PATH} ...")
    rows = load_leaves()
    print(f"Кодов для индексирования: {len(rows):,}")

    if not rows:
        print("Нет данных для индексирования.")
        sys.exit(1)

    # Префиксы (если нужны) добавляет сам бэкенд — они у моделей разные.
    texts = build_index_texts(rows)
    uniq = len(set(texts))
    print(f"Текстов для векторизации: {len(texts):,}, из них уникальных {uniq:,} "
          f"({100 * uniq / len(texts):.1f}%)")
    for row, text in zip(rows, texts):
        row["index_text"] = text

    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"Метаданные сохранены: {META_PATH}")

    # Отпечаток входа: и сами тексты, и модель. Сверять только количество записей
    # нельзя — при смене способа склейки текста количество то же, а векторы уже
    # не те, и старый кэш подхватывался молча.
    fingerprint = {
        "embedder": expected_name(),
        "count": len(texts),
        "texts_sha256": hashlib.sha256(
            "\n".join(texts).encode("utf-8")
        ).hexdigest(),
    }
    stored = None
    if VECS_INFO_PATH.exists():
        with open(VECS_INFO_PATH, encoding="utf-8") as f:
            stored = json.load(f)

    if VECS_PATH.exists() and stored == fingerprint:
        print(f"Векторы уже посчитаны для этих текстов и модели, пропускаем этап embed.")
        print(f"Пересчитать заново — удалите {VECS_PATH}")
        return
    if stored is not None and stored != fingerprint:
        diff = [k for k in fingerprint if stored.get(k) != fingerprint[k]]
        print(f"Кэш векторов устарел (изменилось: {', '.join(diff)}) — считаем заново.")
        if PARTS_DIR.exists():
            for old in PARTS_DIR.glob("part_*.npy"):
                old.unlink()
        VECS_PATH.unlink(missing_ok=True)

    chunks = [(i, texts[i:i + CHUNK_SIZE]) for i in range(0, len(texts), CHUNK_SIZE)]

    PARTS_DIR.mkdir(parents=True, exist_ok=True)
    todo = [(i, ch) for i, ch in chunks if not _part_path(i, len(ch)).exists()]
    done = len(chunks) - len(todo)
    print(f"\nКусков по {CHUNK_SIZE}: всего {len(chunks)}, готово {done}, к расчёту {len(todo)}")

    if todo:
        print(f"Бэкенд векторизации: {backend}")
        embedder = get_embedder()
        print(f"Модель: {embedder.name}")

        for num, (offset, chunk) in enumerate(todo, 1):
            t0 = time.time()
            vecs = embedder.encode(chunk, is_query=False)
            path = _part_path(offset, len(chunk))
            np.save(path, vecs)
            dt = time.time() - t0
            left = (len(todo) - num) * dt
            print(f"  [{num}/{len(todo)}] {path.name}  {dt:.0f}с  "
                  f"({len(chunk)/max(dt, 0.01):.0f} текстов/с, "
                  f"осталось ~{left/60:.0f} мин)", flush=True)

    print("\nСклеиваем куски ...")
    parts = [np.load(_part_path(i, len(ch))) for i, ch in chunks]
    vecs = np.concatenate(parts, axis=0)
    if vecs.shape[0] != len(rows):
        print(f"Не сходится: склеено {vecs.shape[0]}, кодов {len(rows)}. Прерываю.")
        sys.exit(1)

    np.save(VECS_PATH, vecs)
    with open(VECS_INFO_PATH, "w", encoding="utf-8") as f:
        json.dump(fingerprint, f, ensure_ascii=False, indent=2)
    print(f"Векторы сохранены: {VECS_PATH}  {vecs.shape}")
    for i, ch in chunks:
        _part_path(i, len(ch)).unlink()
    PARTS_DIR.rmdir()


def stage_index() -> None:
    """Этап 2: векторы из .npy → FAISS-индекс. torch здесь не импортируем."""
    import faiss
    import numpy as np

    from embedder import expected_name

    if not VECS_PATH.exists():
        print(f"Нет файла векторов: {VECS_PATH}")
        print("Запустите: python build_index.py embed")
        sys.exit(1)

    # В паспорт — модель, которая посчитала векторы (отпечаток этапа embed),
    # а не та, что задана сейчас: `build_index.py index` с другими настройками
    # записал бы чужое имя, и сверка паспорта на старте пропустила бы индекс.
    built_by = None
    if VECS_INFO_PATH.exists():
        with open(VECS_INFO_PATH, encoding="utf-8") as f:
            built_by = json.load(f).get("embedder")
    if not built_by:
        sys.exit(f"Нет отпечатка векторов ({VECS_INFO_PATH.name}) — неизвестно, какой моделью "
                 f"они посчитаны. Пересчитайте: python build_index.py embed")
    if built_by != expected_name():
        print(f"ВНИМАНИЕ: векторы посчитаны моделью «{built_by}», а сейчас задана "
              f"«{expected_name()}». Паспорт пишу по векторам; сервис с текущими "
              f"настройками этот индекс не примет.")

    vecs = np.load(VECS_PATH)
    print(f"Строим FAISS-индекс (IndexFlatIP, dim={vecs.shape[1]}) ...")
    index = faiss.IndexFlatIP(vecs.shape[1])
    index.add(vecs)

    faiss.write_index(index, str(FAISS_PATH))
    print(f"Индекс сохранён: {FAISS_PATH}  ({index.ntotal:,} векторов)")

    # Паспорт индекса: чем собран и в какой размерности. Без него поиск другим
    # бэкендом искал бы в чужом векторном пространстве и молча врал.
    info = {
        "embedder": built_by,
        "dim": int(vecs.shape[1]),
        "count": int(index.ntotal),
    }
    with open(INFO_PATH, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    print(f"Паспорт индекса: {INFO_PATH}  {info}")


def main() -> None:
    stage = sys.argv[1] if len(sys.argv) > 1 else None

    if stage == "embed":
        stage_embed()
        return
    if stage == "index":
        stage_index()
        return
    if stage is not None:
        print(f"Неизвестный этап: {stage}. Допустимо: embed | index")
        sys.exit(2)

    # Без аргументов — гоняем оба этапа, каждый в отдельном процессе.
    for name in ("embed", "index"):
        print(f"\n{'=' * 60}\nЭтап: {name}\n{'=' * 60}")
        # -u обязателен: при падении процесса буферизованный stdout теряется,
        # и в логе не видно, на каком куске всё оборвалось.
        result = subprocess.run(
            [sys.executable, "-u", str(Path(__file__).resolve()), name]
        )
        if result.returncode != 0:
            print(f"\nЭтап {name} упал (код {result.returncode}). Прогон остановлен.")
            sys.exit(result.returncode)

    print("\nГотово. Индекс и метаданные на месте.")


if __name__ == "__main__":
    main()
