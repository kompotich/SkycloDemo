import csv
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence


REPORT_DATE = date(2026, 11, 10)
CENT = Decimal("0.01")
DATA_DIR = Path(__file__).resolve().parent / "data"

WORK_HEADERS = (
    "Договор",
    "work_id",
    "Название работы",
    "Статус работы",
    "Цена работы",
    "Выплачено (по платежам)",
    "Осталось (по платежам)",
)
CONTRACT_HEADERS = (
    "Договор",
    "Сумма договора",
    "Выплачено по договору",
    "Осталось по договору",
)


@dataclass(frozen=True)
class Contract:
    contract_id: str
    amount_cents: int


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    status: str
    price_cents: int


@dataclass(frozen=True)
class Payment:
    contract_id: str
    work_id: str
    amount_cents: int
    status: str
    paid_at: date | None


def parse_cents(value: str, field: str) -> int:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"Некорректная сумма в поле {field}: {value!r}") from error

    if not amount.is_finite() or amount < 0 or amount != amount.quantize(CENT):
        raise ValueError(f"Сумма в поле {field} должна быть неотрицательной и точной до копейки: {value!r}")
    return int(amount * 100)


def read_csv(filename: str, required_columns: Sequence[str]) -> list[dict[str, str]]:
    path = DATA_DIR / filename
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames is None or not set(required_columns).issubset(reader.fieldnames):
                raise ValueError(f"В {path.name} отсутствуют обязательные столбцы.")
            return list(reader)
    except OSError as error:
        raise ValueError(f"Не удалось прочитать файл {path}: {error}") from error


def load_data() -> tuple[list[Contract], list[Work], list[Payment]]:
    contract_rows = read_csv("contracts.csv", ("contract_id", "amount"))
    work_rows = read_csv(
        "works.csv",
        ("work_id", "contract_id", "title", "price", "status"),
    )
    payment_rows = read_csv(
        "payments.csv",
        ("contract_id", "work_id", "amount", "status", "paid_at"),
    )

    contracts = [
        Contract(row["contract_id"], parse_cents(row["amount"], "contracts.amount"))
        for row in contract_rows
    ]
    works = [
        Work(
            row["work_id"],
            row["contract_id"],
            row["title"],
            row["status"],
            parse_cents(row["price"], "works.price"),
        )
        for row in work_rows
    ]
    payments = []
    for row in payment_rows:
        paid_at = date.fromisoformat(row["paid_at"]) if row["paid_at"] else None
        payments.append(
            Payment(
                row["contract_id"],
                row["work_id"],
                parse_cents(row["amount"], "payments.amount"),
                row["status"],
                paid_at,
            )
        )

    validate_data(contracts, works, payments)
    return contracts, works, payments


def validate_data(
    contracts: Sequence[Contract],
    works: Sequence[Work],
    payments: Sequence[Payment],
) -> None:
    contract_ids = [contract.contract_id for contract in contracts]
    if len(contract_ids) != len(set(contract_ids)):
        raise ValueError("В contracts.csv обнаружены повторяющиеся contract_id.")

    work_ids = [work.work_id for work in works]
    if len(work_ids) != len(set(work_ids)):
        raise ValueError("В works.csv обнаружены повторяющиеся work_id.")

    known_contracts = set(contract_ids)
    works_by_id = {work.work_id: work for work in works}
    for work in works:
        if work.contract_id not in known_contracts:
            raise ValueError(f"Работа {work.work_id} ссылается на неизвестный договор {work.contract_id}.")

    for payment in payments:
        if payment.contract_id not in known_contracts:
            raise ValueError(f"Платёж ссылается на неизвестный договор {payment.contract_id}.")
        if payment.work_id:
            work = works_by_id.get(payment.work_id)
            if work is None:
                raise ValueError(f"Платёж ссылается на неизвестную работу {payment.work_id}.")
            if work.contract_id != payment.contract_id:
                raise ValueError(
                    f"Договор платежа {payment.contract_id} не совпадает с договором работы {payment.work_id}."
                )


def allocate_proportionally(total_cents: int, works: Sequence[Work]) -> dict[str, int]:
    eligible = [work for work in works if work.status != "cancelled"]
    total_price = sum(work.price_cents for work in eligible)
    if total_cents and (not eligible or total_price <= 0):
        raise ValueError("Нельзя распределить платеж: у договора нет неотменённых работ с положительной ценой.")

    allocations: dict[str, int] = {}
    remainders: list[tuple[int, int, str]] = []
    allocated_cents = 0
    for index, work in enumerate(eligible):
        base, remainder = divmod(total_cents * work.price_cents, total_price)
        allocations[work.work_id] = base
        allocated_cents += base
        remainders.append((remainder, -index, work.work_id))

    remaining_cents = total_cents - allocated_cents
    for _, _, work_id in sorted(remainders, reverse=True)[:remaining_cents]:
        allocations[work_id] += 1
    return allocations


def build_work_rows(
    contracts: Sequence[Contract],
    works: Sequence[Work],
    payments: Sequence[Payment],
) -> list[list[str]]:
    known_contracts = {contract.contract_id for contract in contracts}
    works_by_contract: dict[str, list[Work]] = {contract_id: [] for contract_id in known_contracts}
    for work in works:
        works_by_contract[work.contract_id].append(work)

    paid_by_work = {work.work_id: 0 for work in works}
    unlinked_paid_by_contract = {contract_id: 0 for contract_id in known_contracts}
    for payment in payments:
        if payment.status != "paid" or payment.paid_at is None or payment.paid_at >= REPORT_DATE:
            continue
        if payment.work_id:
            paid_by_work[payment.work_id] += payment.amount_cents
        else:
            unlinked_paid_by_contract[payment.contract_id] += payment.amount_cents

    for contract_id, amount_cents in unlinked_paid_by_contract.items():
        allocations = allocate_proportionally(amount_cents, works_by_contract[contract_id])
        for work_id, allocated_cents in allocations.items():
            paid_by_work[work_id] += allocated_cents

    rows = []
    for work in works:
        paid_cents = paid_by_work[work.work_id]
        if work.status == "cancelled":
            paid_cents = 0
            remaining_cents = 0
        else:
            remaining_cents = work.price_cents - paid_cents
        rows.append(
            [
                work.contract_id,
                work.work_id,
                work.title,
                work.status,
                format_money(work.price_cents),
                format_money(paid_cents),
                format_money(remaining_cents),
            ]
        )
    return rows


def build_contract_rows(
    contracts: Sequence[Contract],
    works: Sequence[Work],
    payments: Sequence[Payment],
) -> list[list[str]]:
    paid_by_contract = {contract.contract_id: 0 for contract in contracts}
    cancelled_by_contract = {contract.contract_id: 0 for contract in contracts}
    for work in works:
        if work.status == "cancelled":
            cancelled_by_contract[work.contract_id] += work.price_cents

    for payment in payments:
        if payment.status == "paid" and payment.paid_at is not None and payment.paid_at < REPORT_DATE:
            paid_by_contract[payment.contract_id] += payment.amount_cents

    rows = []
    for contract in contracts:
        amount_cents = contract.amount_cents - cancelled_by_contract[contract.contract_id]
        paid_cents = paid_by_contract[contract.contract_id]
        rows.append(
            [
                contract.contract_id,
                format_money(amount_cents),
                format_money(paid_cents),
                format_money(amount_cents - paid_cents),
            ]
        )

    total_amount = sum(
        contract.amount_cents - cancelled_by_contract[contract.contract_id]
        for contract in contracts
    )
    total_paid = sum(paid_by_contract.values())
    rows.append(
        [
            "Итого",
            format_money(total_amount),
            format_money(total_paid),
            format_money(total_amount - total_paid),
        ]
    )
    return rows


def format_money(cents: int) -> str:
    amount = Decimal(cents) / 100
    formatted = f"{amount:,.2f}"
    return formatted.replace(",", " ").replace(".", ",")


def print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]
    print(" | ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def save_csv(filename: str, headers: Sequence[str], rows: Iterable[Sequence[str]]) -> Path:
    path = Path(__file__).resolve().parent / filename
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file, delimiter=";")
        writer.writerow(headers)
        writer.writerows(rows)
    return path


def main() -> int:
    print("Введите команду для отчёта на 10.11.2026:")
    print("  show_works       — показать отчёт по работам")
    print("  show_works /s    — показать отчёт по работам и сохранить CSV")
    print("  show_contracts   — показать отчёт по договорам")
    print("  show_contracts /s — показать отчёт по договорам и сохранить CSV")
    try:
        command = input("> ").strip().split()
    except EOFError:
        print("Ввод завершён до выбора отчёта.", file=sys.stderr)
        return 2

    if command not in (
        ["show_works"],
        ["show_works", "/s"],
        ["show_contracts"],
        ["show_contracts", "/s"],
    ):
        print("Неизвестная команда. Введите один из перечисленных вариантов.", file=sys.stderr)
        return 2

    report = command[0]
    save = len(command) == 2

    try:
        contracts, works, payments = load_data()
        work_rows = build_work_rows(contracts, works, payments)
        if report == "show_works":
            headers = WORK_HEADERS
            rows = work_rows
            filename = "works_report.csv"
        else:
            headers = CONTRACT_HEADERS
            rows = build_contract_rows(contracts, works, payments)
            filename = "contracts_report.csv"

        print_table(headers, rows)
        if save:
            saved_path = save_csv(filename, headers, rows)
            print(f"\nCSV сохранён: {saved_path.name}")
        input()
    except (OSError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
