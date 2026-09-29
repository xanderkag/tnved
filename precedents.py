"""Прецеденты ФСА — какие позиции заявляли у похожих товаров (ALG-5, шаг 1 навигатора).

Открытые данные Росаккредитации (fsa.gov.ru/opendata, реестры деклараций и сертификатов
соответствия): у записи есть наименование товара и заявленный код ТН ВЭД. Код ставит
заявитель, его никто не проверяет, поэтому это подсказка позиции, а не ответ. Замер 29.09
на наборе ЕЭК (195 решений, 4 знака): по короткому наименованию позиция из прецедентов верна
в 34% случаев первой и в 54% входит в первые три; поиск по дереву — 13% и 22%.

Свод — таблица TSV (можно .gz) с колонками name_norm, code, n: наименование после
нормализации, код и сколько записей его заявили. Собирается на asha из выгрузок ФСА: в них есть
заявители и ИНН, а в свод попадают только поля товара. В git и образ свод не входит.

Путь — PRECEDENTS_PATH. Не задан — уровень выключен. Задан, но файл не читается или нужных
колонок нет — сервис не стартует, как и с DECLARATIONS_PATH.

Поиск — TF-IDF по основам слов (первые 6 букв) без сторонних библиотек: инвертированный
индекс в массивах numpy. Ближайшие PRECEDENTS_NEIGHBOURS наименований голосуют за позиции
с весом «сходство² × доля кода у наименования».
"""
from __future__ import annotations

import csv
import gzip
import logging
import math
import os
import re
from array import array
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

log = logging.getLogger("tnved.precedents")

REQUIRED = ("name_norm", "code", "n")
STEM = 6
NEIGHBOURS_DEFAULT = 50
MIN_DF = 2
_WORD = re.compile(r"[а-яa-z0-9]+")
_STOP = frozenset("для из и в с на по без не от до при или а к о об же под над все его ее их".split())


def stems(text: str) -> list[str]:
    """Основы слов: нижний регистр, ё → е, слова от 3 букв без служебных, первые STEM букв."""
    words = _WORD.findall(str(text or "").lower().replace("ё", "е"))
    return [w[:STEM] for w in words if len(w) > 2 and w not in _STOP]


class PrecedentIndex:
    """Свод «наименование → заявленные позиции» с поиском похожих наименований.

    Всё хранится в плоских массивах numpy: на 390 тыс. наименований словари и списки
    кортежей занимали при загрузке 2 ГБ.
    """

    def __init__(self, rows, neighbours: int = NEIGHBOURS_DEFAULT):
        self.neighbours = neighbours
        heads: dict[str, Counter] = defaultdict(Counter)  # наименование → позиция → записей
        for row in rows:
            code = re.sub(r"\D", "", str(row.get("code") or ""))
            name = str(row.get("name_norm") or "").strip()
            if len(code) == 10 and name:
                heads[name][code[:4]] += int(row.get("n") or 1)
        self.names = list(heads)

        # позиции наименования и их доля: голосуем позицией, 10 знаков решает дерево
        self.heading_list: list[str] = []
        heading_id: dict[str, int] = {}
        share_start = array("q", [0])
        share_heading, share_value = array("i"), array("f")
        for name in self.names:
            total = sum(heads[name].values())
            for h, n in heads[name].items():
                share_heading.append(heading_id.setdefault(h, len(heading_id)))
                share_value.append(n / total)
            share_start.append(len(share_heading))
        self.heading_list = list(heading_id)
        del heads
        self.share_start = np.frombuffer(share_start, dtype=np.int64)
        self.share_heading = np.frombuffer(share_heading, dtype=np.int32)
        self.share_value = np.frombuffer(share_value, dtype=np.float32)

        # термины наименований: (документ, термин, частота) — плоско
        vocab: dict[str, int] = {}
        doc_ids, term_ids, tfs = array("i"), array("i"), array("f")
        for doc_id, name in enumerate(self.names):
            for t, n in Counter(stems(name)).items():
                doc_ids.append(doc_id)
                term_ids.append(vocab.setdefault(t, len(vocab)))
                tfs.append(n)
        doc_ids = np.frombuffer(doc_ids, dtype=np.int32)
        term_ids = np.frombuffer(term_ids, dtype=np.int32)
        tfs = np.frombuffer(tfs, dtype=np.float32)
        df = np.bincount(term_ids, minlength=len(vocab))
        keep_term = df >= MIN_DF
        # новые номера терминов: только с df >= MIN_DF
        remap = np.full(len(vocab), -1, dtype=np.int32)
        remap[keep_term] = np.arange(int(keep_term.sum()), dtype=np.int32)
        self.vocab = {t: int(remap[i]) for t, i in vocab.items() if keep_term[i]}
        # idf как в sklearn (smooth_idf): ln((1+N)/(1+df)) + 1
        n_docs = len(self.names)
        self.idf = (np.log((1 + n_docs) / (1 + df[keep_term])) + 1).astype(np.float32)
        mask = keep_term[term_ids]
        doc_ids, term_ids, tfs = doc_ids[mask], remap[term_ids[mask]], tfs[mask]
        weights = (1 + np.log(tfs)) * self.idf[term_ids]
        norms = np.sqrt(np.bincount(doc_ids, weights=weights * weights, minlength=n_docs))
        weights = (weights / np.maximum(norms[doc_ids], 1e-12)).astype(np.float32)
        # инвертированный индекс: для термина — отрезок [start, end) в doc_ids/weights
        order = np.argsort(term_ids, kind="stable")
        self.doc_ids = doc_ids[order]
        self.weights = weights[order]
        self.starts = np.zeros(len(self.vocab) + 1, dtype=np.int64)
        np.cumsum(np.bincount(term_ids, minlength=len(self.vocab)), out=self.starts[1:])
        log.info("прецеденты ФСА: %d наименований, %d основ", n_docs, len(self.vocab))

    def _query(self, text: str) -> dict[int, float]:
        tf = Counter(stems(text))
        w = {self.vocab[t]: (1 + math.log(n)) * self.idf[self.vocab[t]] for t, n in tf.items() if t in self.vocab}
        norm = math.sqrt(sum(v * v for v in w.values())) or 1.0
        return {i: v / norm for i, v in w.items()}

    def similar(self, text: str, k: int | None = None) -> list[tuple[int, float]]:
        """До k ближайших наименований: [(номер, косинус)] по убыванию сходства."""
        k = k or self.neighbours
        q = self._query(text)
        if not q:
            return []
        scores = np.zeros(len(self.names), dtype=np.float32)
        for i, qv in q.items():
            s, e = self.starts[i], self.starts[i + 1]
            np.add.at(scores, self.doc_ids[s:e], qv * self.weights[s:e])
        k = min(k, int((scores > 0).sum()))
        if k == 0:
            return []
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]
        return [(int(i), float(scores[i])) for i in top]

    def headings(self, text: str, valid: set[str] | None = None, limit: int = 10) -> list[tuple[str, float]]:
        """Позиции (4 знака) по голосам похожих наименований: [(позиция, доля голосов)].

        valid — позиции, у которых есть действующие коды (store.current_headings): прочие
        отбрасываем, как и позиции от triage.
        """
        vote: Counter = Counter()
        for i, sim in self.similar(text):
            for j in range(self.share_start[i], self.share_start[i + 1]):
                heading = self.heading_list[self.share_heading[j]]
                if valid is None or heading in valid:
                    vote[heading] += sim * sim * float(self.share_value[j])
        total = sum(vote.values())
        return [(h, v / total) for h, v in vote.most_common(limit)] if total else []


def load(path: str | Path, neighbours: int = NEIGHBOURS_DEFAULT) -> PrecedentIndex:
    """Свод из TSV (.gz); нет файла или колонок — исключение: сервис с таким путём не стартует."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: нет колонок {', '.join(missing)}")
        return PrecedentIndex(reader, neighbours=neighbours)


def load_from_env() -> PrecedentIndex | None:
    path = os.environ.get("PRECEDENTS_PATH", "").strip()
    if not path:
        return None
    raw = os.environ.get("PRECEDENTS_NEIGHBOURS", "").strip()
    neighbours = int(raw) if raw else NEIGHBOURS_DEFAULT
    if not 1 <= neighbours <= 500:
        raise ValueError(f"PRECEDENTS_NEIGHBOURS={raw}: нужно целое от 1 до 500")
    return load(path, neighbours)
