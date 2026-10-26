from flask import Flask, request, redirect, url_for, session, render_template_string, abort, flash
import sqlite3, os, re, json
from datetime import datetime
import requests

app = Flask(__name__)
app.secret_key = "change-this-demo-secret"
DB_PATH = os.environ.get("DB_PATH", "credit.db")
DADATA_API_KEY = "011fbbfe4cd0b25f979619dd59d779a1bcc868a6"

STATUSES = {
    "draft": "Черновик", "submitted": "Подана", "checking": "Проверка данных",
    "review": "На рассмотрении", "more_info": "Нужны уточнения",
    "approved": "Одобрена", "offer": "Предложены другие условия",
    "rejected": "Отказ", "signed": "Документы оформлены", "funded": "Финансирование выдано"
}

def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with connect() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS applications (
          id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL, inn TEXT NOT NULL,
          contact TEXT NOT NULL, product TEXT NOT NULL, amount INTEGER NOT NULL,
          term INTEGER NOT NULL, purpose TEXT NOT NULL, revenue INTEGER NOT NULL,
          expenses INTEGER NOT NULL, debt INTEGER NOT NULL, overdue INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'draft', score INTEGER, risk TEXT, decision TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        if con.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0:
            now = datetime.now().isoformat(timespec="minutes")
            demo = [
              ("ООО «Северный кофе»","7701001001","Мария Смирнова","Оборотный кредит",1200000,18,"Закупка сырья и упаковки",2500000,1750000,200000,0,"review",82,"Низкий","Заявка ожидает проверки сотрудником"),
              ("ИП Иванов","5002002002","Алексей Иванов","Кредит на оборудование",2800000,36,"Покупка оборудования",1800000,1450000,600000,0,"more_info",61,"Средний","Нужно уточнить ежемесячные расходы"),
              ("ООО «Вектор»","7803003003","Ольга Петрова","Оборотный кредит",900000,12,"Пополнение оборотных средств",950000,890000,750000,1,"rejected",28,"Высокий","Демо-отказ: высокая долговая нагрузка и отметка о просрочке")
            ]
            con.executemany("""INSERT INTO applications
              (company,inn,contact,product,amount,term,purpose,revenue,expenses,debt,overdue,status,score,risk,decision,created_at,updated_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", [(*x,now,now) for x in demo])

def get_app(app_id):
    with connect() as con:
        return con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
def calculate_score(a):

    def val(key, default=0):
        try:
            value = a[key]
            return default if value is None else float(value)
        except (KeyError, IndexError, TypeError, ValueError):
            return float(default)

    try:
        extra = json.loads(a["extra_data"] or "{}")
    except (KeyError, TypeError, ValueError):
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    revenue = val("revenue")
    expenses = val("expenses")
    try:
        profit = float(extra.get("profit", revenue - expenses))
    except (TypeError, ValueError):
        profit = revenue - expenses
    existing_payment = val("debt")
    amount = val("amount")
    term = max(1.0, val("term", 1))
    cash_flow = profit
    estimated_new_payment = amount / term
    total_debt_payments = existing_payment + estimated_new_payment
    margin = profit / revenue if revenue > 0 else -1
    dscr = cash_flow / total_debt_payments if total_debt_payments > 0 else (99.0 if cash_flow > 0 else 0.0)
    existing_debt_ratio = existing_payment / revenue if revenue > 0 else 99.0
    loan_to_monthly_revenue = amount / revenue if revenue > 0 else 99.0

    profitability = 15 if margin >= .20 else 12 if margin >= .10 else 8 if margin >= .05 else 4 if margin >= 0 else 0
    dscr_points = 25 if dscr >= 1.5 else 20 if dscr >= 1.25 else 14 if dscr >= 1.0 else 7 if dscr >= .75 else 0

    monthly = extra.get("monthly_revenue", [])
    values = [float(x.get("revenue", 0)) for x in monthly if isinstance(x, dict) and x.get("revenue") is not None]
    if len(values) >= 6 and sum(values) > 0:
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / len(values)
        cv = (variance ** .5) / mean
        stability = 20 if cv <= .15 else 15 if cv <= .30 else 10 if cv <= .50 else 5 if cv <= .75 else 0
        stability_note = f"Коэффициент вариации выручки: {cv:.2f} по {len(values)} месяцам"
    else:
        stability = 0
        stability_note = "Недостаточно помесячных данных для оценки стабильности выручки"

    debt_burden = 15 if existing_debt_ratio <= .10 else 12 if existing_debt_ratio <= .25 else 8 if existing_debt_ratio <= .40 else 4 if existing_debt_ratio <= .60 else 0
    age_points = 0
    age_note = "Дата регистрации не указана"
    try:
        reg_date = extra.get("reg_date")
        if reg_date:
            registered = datetime.strptime(reg_date, "%Y-%m-%d").date()
            days = (datetime.now().date() - registered).days
            age_points = 5 if days >= 1095 else 3 if days >= 365 else 1
            age_note = f"Возраст бизнеса: около {max(0, days // 30)} мес."
    except (TypeError, ValueError):
        pass

    loan_size = 10 if loan_to_monthly_revenue <= 3 else 8 if loan_to_monthly_revenue <= 6 else 5 if loan_to_monthly_revenue <= 12 else 2 if loan_to_monthly_revenue <= 24 else 0
    term_points = 10 if term <= 12 else 8 if term <= 24 else 5 if term <= 36 else 2
    score = int(round(max(0, min(100, profitability + dscr_points + stability + debt_burden + age_points + loan_size + term_points))))
    risk = "Низкий" if score >= 75 else "Средний" if score >= 50 else "Высокий"

    details = {
        "score_version": "heuristic_v2",
        "score_type": "предварительный балл, не вероятность дефолта",
        "components": [
            {"name": "Рентабельность", "points": profitability, "max": 15, "detail": f"Маржа по введённой чистой прибыли: {margin:.1%}" if revenue > 0 else "Выручка равна нулю — маржу оценить нельзя"},
            {"name": "Покрытие платежей (DSCR)", "points": dscr_points, "max": 25, "detail": f"{dscr:.2f}; свободный денежный поток / существующие и оценочные новые платежи"},
            {"name": "Стабильность выручки", "points": stability, "max": 20, "detail": stability_note},
            {"name": "Текущая долговая нагрузка", "points": debt_burden, "max": 15, "detail": f"Существующие ежемесячные платежи / выручка: {existing_debt_ratio:.1%}" if revenue > 0 else "Нельзя оценить без положительной выручки"},
            {"name": "Срок работы бизнеса", "points": age_points, "max": 5, "detail": age_note},
            {"name": "Размер кредита относительно выручки", "points": loan_size, "max": 10, "detail": f"Сумма кредита / месячная выручка: {loan_to_monthly_revenue:.1f}x" if revenue > 0 else "Нельзя оценить без положительной выручки"},
            {"name": "Срок кредитования", "points": term_points, "max": 10, "detail": f"Запрошенный срок: {int(term)} мес."},
        ],
        "estimated_new_monthly_payment": round(estimated_new_payment, 2),
        "existing_monthly_debt_payments": round(existing_payment, 2),
        "credit_history": "не проверена: интеграция с бюро кредитных историй не подключена",
        "business_registry": "не проверен: интеграция с официальными реестрами не подключена",
        "limitations": ["Правила экспертные и не обучены на истории дефолтов.", "Платёж по новому кредиту рассчитан как сумма / срок и не включает проценты, комиссии и график погашения.", "Баллы нельзя трактовать как вероятность дефолта или автоматическое решение банка."],
    }
    return score, risk, details

def score_app(a):
    score, risk, _ = calculate_score(a)
    return score, risk

def validate_company_data(inn, company_name, product):
    if not DADATA_API_KEY:
        return {
            "success": False,
            "error": "Не задан DADATA_API_KEY. Проверка по ИНН недоступна (демо).",
            "official_name": None, "official_activity": None, "status": None,
            "name_matches": False, "warning": None
        }

    url = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {DADATA_API_KEY}"
    }
    payload = {"query": inn, "branch_type": "MAIN"}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        return {
            "success": False,
            "error": f"Ошибка при обращении: {e}",
            "official_name": None, "official_activity": None, "status": None,
            "name_matches": False, "warning": None
        }

    suggestions = data.get("suggestions", [])
    if not suggestions:
        return {
            "success": False,
            "error": "ИНН не найден в базе данных.",
            "official_name": None, "official_activity": None, "status": None,
            "name_matches": False, "warning": None
        }

    company_data = suggestions[0].get("data", {})
    official_name = company_data.get("name", {}).get("full_with_opf", "") or ""
    okved_code = company_data.get("okved", "") or ""
    status = company_data.get("state", {}).get("status", "UNKNOWN") or "UNKNOWN"

    clean_input = re.sub(r'[«»"\']', '', company_name).lower().strip()
    clean_official = re.sub(r'[«»"\']', '', official_name).lower().strip()
    name_matches = bool(clean_official) and (clean_input in clean_official or clean_official in clean_input)

    warning = None
    if status in ("LIQUIDATING", "LIQUIDATED", "BANKRUPT"):
        warning = f"Внимание! Статус компании: {status}. Это может быть рискованно."
    elif not name_matches:
        warning = f"Введенное название отличается от официального: {official_name}"

    return {
        "success": True, "error": None,
        "official_name": official_name,
        "official_activity": f"ОКВЭД: {okved_code}" if okved_code else "Вид деятельности не определён",
        "status": status, "name_matches": name_matches, "warning": warning
    }

def page(title, body, role=None):
    return render_template_string(TEMPLATE, title=title, body=body, role=role, statuses=STATUSES)

TEMPLATE = r"""
<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{title}} · Кредитный кабинет</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,500;0,600;0,700;1,400&family=Inter:wght@300;400;500;600;700&family=Marck+Script&display=swap" rel="stylesheet">
<style>
:root{--cream:#ffe4c9;--crimson:#ff0828;--burgundy:#6b000d;--darkburgundy:#240205;--black:#000000;--navy:var(--burgundy);--red:var(--crimson);--muted:rgba(255,228,201,.68);--line:rgba(255,228,201,.18);--serif:'Cormorant Garamond',Georgia,serif;--sans:'Inter',Arial,sans-serif;--script:'Marck Script',cursive}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(circle at 75% 12%,rgba(107,0,13,.45) 0%,transparent 48%),radial-gradient(circle at 15% 65%,rgba(80,0,10,.35) 0%,transparent 55%),linear-gradient(135deg,#1f0004 0%,#0c0002 100%);font-family:var(--sans);color:var(--cream);min-height:100vh;display:flex;flex-direction:column;-webkit-font-smoothing:antialiased}
header{background:linear-gradient(90deg,#b80018 0%,var(--burgundy) 42%,#1c0003 76%,var(--black) 100%);backdrop-filter:blur(12px);color:var(--cream);padding:20px max(24px, calc((100% - 1180px)/2));display:flex;justify-content:space-between;gap:20px;align-items:center;border-bottom:1px solid rgba(255,228,201,.22);box-shadow:0 8px 28px rgba(0,0,0,.28);position:sticky;top:0;z-index:100}
.brand{font-family:var(--serif);font-weight:600;font-size:22px;letter-spacing:3px;text-transform:uppercase;color:var(--cream)}.brand span{color:var(--cream);opacity:.7;padding:0 4px}
.nav{display:flex;gap:24px;flex-wrap:wrap;align-items:center}.nav a{color:var(--cream);text-decoration:none;font-size:11px;font-weight:500;letter-spacing:2px;text-transform:uppercase;opacity:.85;transition:.2s}.nav a:hover{opacity:1;color:var(--crimson)}
main{max-width:1180px;width:100%;margin:0 auto;padding:36px 24px 70px;flex:1}
.hero{position:relative;overflow:hidden;padding:102px 42px 46px;border-radius:4px;background:radial-gradient(circle at 68% 35%,rgba(255,8,40,.16) 0%,rgba(107,0,13,.72) 38%,rgba(0,0,0,.96) 100%),linear-gradient(135deg,#2b0005 0%,#000 100%);border:1px solid rgba(107,0,13,.35);margin-bottom:34px;box-shadow:0 20px 50px rgba(0,0,0,.35);color:var(--cream)}
.hero::before{content:"CREDIT CABINET";position:absolute;top:2px;left:36px;right:36px;font-family:var(--serif);font-size:clamp(42px,8.8vw,112px);font-weight:400;letter-spacing:2px;line-height:.92;color:rgba(255,228,201,.13);pointer-events:none;white-space:nowrap;z-index:0}
.hero::after{content:"";position:absolute;right:88px;top:56%;transform:translateY(-50%);width:210px;height:210px;border-radius:50%;background:url('/static/лого.png') center/116% no-repeat,url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 500 500'><defs><linearGradient id='g' x1='0' y1='0' x2='0' y2='1'><stop offset='0%' stop-color='%23a80514'/><stop offset='52%' stop-color='%23590007'/><stop offset='86%' stop-color='%23140002'/><stop offset='100%' stop-color='%23000000'/></linearGradient><path id='p' d='M 44,250 A 206,206 0 0,0 456,250'/></defs><circle cx='250' cy='250' r='248' fill='url(%23g)'/><circle cx='250' cy='250' r='230' fill='none' stroke='%23ffffff' stroke-width='3' opacity='0.9'/><g fill='%23ffffff' font-family='Georgia,serif' text-anchor='middle'><text x='250' y='178' font-size='68' letter-spacing='2'>SMART</text><text x='250' y='258' font-size='68' letter-spacing='2'>CREDIT</text><text x='250' y='338' font-size='66' letter-spacing='1'>CABINET</text></g><text fill='%23ffffff' font-family='Arial,sans-serif' font-size='24' letter-spacing='0.5'><textPath href='%23p' startOffset='50%' text-anchor='middle'>%23акулы_бизнеса|alpha_business</textPath></text></svg>") center/cover no-repeat;box-shadow:0 14px 36px rgba(0,0,0,.75);pointer-events:none;z-index:1}
.hero>*{position:relative;z-index:2}
.eyebrow{font-size:11px;color:var(--crimson);font-weight:600;letter-spacing:3px;text-transform:uppercase;display:inline-flex;align-items:center;gap:10px}.eyebrow::before{content:"✦";color:var(--cream);font-size:10px}
.hero h1{font-family:var(--serif);font-weight:400;font-size:clamp(34px,5.2vw,60px);text-transform:uppercase;letter-spacing:1.5px;line-height:1.04;margin:14px 0 18px;color:var(--cream);max-width:740px}
.hero p{color:var(--muted);font-size:14px;font-weight:300;line-height:1.7;max-width:580px;margin:0;border-left:1px solid var(--crimson);padding-left:16px}
h2,h3{font-family:var(--serif);font-weight:600;text-transform:uppercase;letter-spacing:1.5px;color:var(--cream)}
.card h2,.card h3,.card h1{color:var(--cream);font-weight:500}
h2{font-size:clamp(25px,3vw,34px);margin:26px 0 16px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}h3{font-size:22px;margin:14px 0 10px}
.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:22px;counter-reset:ed}
.hero+.grid:not(:has(.metric)){background:linear-gradient(125deg,var(--burgundy) 0%,#2a0005 58%,var(--black) 100%);color:var(--cream);padding:36px 32px;border-radius:4px;border:1px solid rgba(107,0,13,.45);box-shadow:0 24px 60px rgba(0,0,0,.45);gap:24px}
.hero+.grid:not(:has(.metric)) .card{background:transparent;color:var(--cream);border:0;border-right:1px solid var(--line);border-radius:0;padding:0 22px 0 0;box-shadow:none;display:flex;flex-direction:column;justify-content:space-between}
.hero+.grid:not(:has(.metric)) .card:last-child{border-right:0;padding-right:0}
.hero+.grid:not(:has(.metric)) .card::after{counter-increment:ed;content:"0" counter(ed);display:block;margin-top:18px;padding-top:12px;border-top:1px solid var(--line);font-family:var(--serif);font-size:22px;font-weight:600;color:var(--cream)}
.hero+.grid:not(:has(.metric)) .card h2{color:var(--cream);font-size:24px;margin:12px 0 8px}
.hero+.grid:not(:has(.metric)) .card .muted,.hero+.grid:not(:has(.metric)) .card .small{color:var(--muted)}
.hero+.grid:not(:has(.metric)) .card .pill{align-self:flex-start;background:rgba(0,0,0,.45);color:var(--cream);border-color:rgba(255,228,201,.28)}
.hero+.grid:not(:has(.metric)) .card .btn{margin-top:14px;background:linear-gradient(135deg,#a80016 0%,var(--burgundy) 100%);color:var(--cream);border-color:rgba(255,228,201,.38)}
.hero+.grid:not(:has(.metric)) .card .btn:hover{background:var(--crimson);border-color:var(--crimson);color:var(--cream)}
.grid:has(.metric){background:linear-gradient(125deg,var(--burgundy) 0%,#2a0005 58%,var(--black) 100%);border:1px solid rgba(107,0,13,.4);border-radius:4px;padding:26px 14px;gap:0;margin-bottom:26px!important;box-shadow:0 18px 45px rgba(0,0,0,.35)}
.grid:has(.metric) .card{background:transparent;border:0;border-right:1px solid rgba(255,228,201,.2);border-radius:0;box-shadow:none;padding:6px 26px}.grid:has(.metric) .card:last-child{border-right:0}
.metric{font-family:var(--serif);font-size:50px;font-weight:400;line-height:1;color:var(--cream);margin:8px 0}
.card .grid{background:rgba(107,0,13,.28);border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:14px 0;margin:14px 0!important;gap:0}
.card .grid>div{padding:0 16px;border-right:1px solid var(--line)}.card .grid>div:first-child{padding-left:4px}.card .grid>div:last-child{border-right:0}
.card .grid b{font-family:var(--serif);font-size:23px;font-weight:600;color:var(--cream)}
.two{display:grid;grid-template-columns:1.1fr .9fr;gap:22px}
.card{background:linear-gradient(125deg,var(--burgundy) 0%,#2a0005 58%,var(--black) 100%);color:var(--cream);border:1px solid rgba(107,0,13,.4);border-radius:4px;padding:26px;box-shadow:0 14px 36px rgba(0,0,0,.35);min-width:0;transition:.25s}
.card:hover{border-color:rgba(107,0,13,.75)}.card h2,.card h3{margin-top:0;overflow-wrap:anywhere}
.muted{color:var(--muted);font-size:14px;line-height:1.6;font-weight:300}.small{font-size:11px;color:var(--muted);letter-spacing:1.4px;text-transform:uppercase}
.pill{display:inline-flex;align-items:center;justify-content:center;max-width:100%;padding:6px 12px;border-radius:2px;background:var(--burgundy);border:1px solid var(--line);color:var(--cream);font-size:10px;font-weight:600;letter-spacing:1.8px;text-transform:uppercase;line-height:1.3;text-align:center;white-space:normal;overflow-wrap:anywhere;vertical-align:middle}
.status-pill{min-height:28px;flex-shrink:0}.status-draft{background:rgba(255,228,201,.12);color:var(--cream)}
.status-submitted,.status-checking,.status-review,.status-more_info{background:rgba(107,0,13,.85);color:var(--cream);border-color:rgba(255,228,201,.4)}
.status-approved,.status-offer,.status-signed,.status-funded,.green{background:var(--cream);color:var(--black);border-color:var(--cream)}
.status-rejected,.redpill{background:rgba(255,8,40,.2);color:var(--crimson);border-color:var(--crimson)}.orange{background:var(--burgundy);color:var(--cream);border-color:rgba(255,228,201,.4)}
.btn{display:inline-flex;justify-content:center;align-items:center;padding:12px 20px;border:1px solid var(--crimson);border-radius:2px;background:linear-gradient(135deg,var(--crimson) 0%,#b80018 100%);color:var(--cream);text-decoration:none;font-size:11px;font-weight:600;letter-spacing:2px;text-transform:uppercase;cursor:pointer;transition:.2s}
.btn:hover{background:var(--burgundy);border-color:var(--cream);color:var(--cream);transform:translateY(-1px);box-shadow:0 6px 20px rgba(107,0,13,.45)}
.btn.dark{background:linear-gradient(135deg,#8f0012 0%,var(--burgundy) 100%);border-color:rgba(255,228,201,.45);color:var(--cream)}.btn.dark:hover{background:var(--crimson);border-color:var(--crimson);color:var(--cream)}
.btn.secondary{background:linear-gradient(135deg,#7d0010 0%,#4a0009 100%);border:1px solid rgba(255,228,201,.38);color:var(--cream)}.btn.secondary:hover{border-color:var(--crimson);background:var(--crimson);color:var(--cream)}
.actions{display:flex;gap:12px;flex-wrap:wrap;align-items:center;min-width:0;margin-top:18px}.actions .pill{max-width:100%}
form{display:grid;gap:15px}.formgrid{display:grid;grid-template-columns:1fr 1fr;gap:15px}.full{grid-column:1/-1}
label{display:grid;gap:8px;font-size:11px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--cream)}
input,select{width:100%;font:inherit;font-size:14px;color:var(--cream);border:1px solid var(--line);border-radius:2px;padding:12px 14px;background:rgba(0,0,0,.62)}
input::placeholder{color:rgba(255,228,201,.35)}select option{background:var(--black);color:var(--cream)}
input:focus,select:focus{outline:0;border-color:var(--crimson);box-shadow:0 0 0 2px rgba(255,8,40,.22)}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;padding:13px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:10px;letter-spacing:1.8px;text-transform:uppercase;color:var(--muted);font-weight:600}td a{color:var(--crimson);text-decoration:none;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;font-weight:600}td a:hover{color:var(--cream)}.tablewrap{overflow:auto}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:14px;margin-top:28px;padding-top:20px;border-top:1px solid var(--line)}
.step{position:relative;padding:32px 10px 8px 0;background:transparent;font-size:11px;font-weight:500;letter-spacing:1px;text-transform:uppercase;color:var(--muted)}
.step::before{content:"✦";position:absolute;top:0;left:0;width:24px;height:24px;border-radius:50%;border:1px solid var(--line);background:var(--black);color:var(--muted);display:flex;align-items:center;justify-content:center;font-size:10px;z-index:2}
.step::after{content:"";position:absolute;top:12px;left:24px;right:0;height:1px;background:var(--line);z-index:1}.step:last-child::after{display:none}
.step.active{color:var(--cream);font-weight:700}.step.active::before{border-color:var(--crimson);background:var(--crimson);color:var(--cream);box-shadow:0 0 14px rgba(255,8,40,.65)}
.step.done{color:var(--cream)}.step.done::before{content:"✓";border-color:var(--cream);background:var(--burgundy);color:var(--cream)}.step.done::after{background:rgba(255,228,201,.45)}
.notice{padding:14px 18px;border-radius:2px;background:rgba(107,0,13,.32);border-left:3px solid var(--crimson);color:var(--cream);font-size:13px;line-height:1.6;margin:15px 0}
.flash{padding:14px 18px;background:var(--burgundy);border:1px solid var(--crimson);color:var(--cream);border-radius:2px;margin-bottom:18px;font-size:13px}
.riskbar{height:8px;background:rgba(255,228,201,.12);overflow:hidden;margin:10px 0}.riskbar span{display:block;height:100%;background:linear-gradient(90deg,var(--burgundy),var(--crimson))}
footer{max-width:1180px;width:calc(100% - 48px);margin:0 auto 36px;background:linear-gradient(125deg,var(--burgundy) 0%,#2a0005 60%,var(--black) 100%);border:1px solid rgba(107,0,13,.4);color:var(--muted);font-size:11px;letter-spacing:1.5px;text-transform:uppercase;padding:32px 36px;display:flex;flex-direction:column;gap:8px;box-shadow:0 16px 40px rgba(0,0,0,.3)}
footer::before{content:"LET'S CREATE SOMETHING";font-family:var(--serif);font-size:clamp(22px,3vw,32px);color:var(--cream);letter-spacing:2px;line-height:1.1}
footer::after{content:"#акулы_бизнеса ";font-family:var(--script);font-size:clamp(30px,4vw,44px);color:var(--crimson);text-transform:none;letter-spacing:0;margin:-12px 0 6px}
@media(max-width:860px){header{flex-direction:column;align-items:flex-start}.hero{padding:64px 24px 32px}.hero::after{display:none}.grid,.two,.formgrid{grid-template-columns:1fr}.hero+.grid:not(:has(.metric)) .card,.grid:has(.metric) .card{border-right:0;border-bottom:1px solid var(--line);padding:0 0 20px}.hero+.grid:not(:has(.metric)) .card:last-child,.grid:has(.metric) .card:last-child{border-bottom:0;padding-bottom:0}main{padding:20px 16px 45px}}
</style></head><body><header><div class="brand">α ALFA <span>·</span> Кредитный кабинет</div><nav class="nav">
{% if role == 'customer' %}<a href="{{url_for('customer')}}">Мой кабинет</a><a href="{{url_for('new_application')}}">Новая заявка</a>{% elif role == 'bank' %}<a href="{{url_for('bank_dashboard')}}">Рабочее место банка</a>{% endif %}<a href="{{url_for('switch_role')}}">Сменить роль</a></nav></header>
<main>{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}{{body|safe}}</main>
<footer>Демо-прототип · синтетические данные · скоринг не является реальным банковским решением</footer></body></html>
"""

@app.route("/")
def index():
    if session.get("role") == "customer": return redirect(url_for("customer"))
    if session.get("role") == "bank": return redirect(url_for("bank_dashboard"))
    body = """<section class="hero"><div class="eyebrow">От заявки до решения — в одном месте</div><h1>Финансирование бизнеса<br>без лишней неопределённости</h1><p>Предприниматель оформляет заявку и отслеживает этапы, а сотрудник банка проверяет данные и фиксирует решение в едином цифровом процессе.</p></section>
    <div class="grid"><div class="card"><span class="pill">Клиентский сценарий</span><h2 style="margin-top:14px">Кабинет предпринимателя</h2><p class="muted">Заявки, статусы, скоринг и дальнейшие действия.</p><a class="btn" href="/role/customer">Войти как клиент</a></div>
    <div class="card"><span class="pill">Внутренний процесс</span><h2 style="margin-top:14px">Рабочее место банка</h2><p class="muted">Очередь, оценка факторов риска и управление статусами.</p><a class="btn dark" href="/role/bank">Войти как сотрудник</a></div>
    <div class="card"><span class="pill green">Готовые сценарии</span><h2 style="margin-top:14px">Можно проверить сразу</h2><p class="muted">Три тестовые заявки с разными результатами рассмотрения.</p><p class="small">Реальные банковские системы не подключаются.</p></div></div>"""
    return page("Главная", body)

@app.route("/role/<role>")
def set_role(role):
    if role not in ("customer","bank"): abort(404)
    session["role"] = role
    return redirect(url_for("customer" if role == "customer" else "bank_dashboard"))

@app.route("/switch")
def switch_role():
    session.pop("role", None)
    return redirect(url_for("index"))

@app.route("/customer")
def customer():
    if session.get("role") != "customer": return redirect(url_for("index"))
    with connect() as con: rows = con.execute("SELECT * FROM applications ORDER BY updated_at DESC").fetchall()
    cards = ""
    for a in rows:
        cards += f"""<div class="card"><div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap"><div><span class="small">Заявка №{a['id']}</span><h3 style="margin:6px 0">{a['company']}</h3></div><span class="pill status-pill status-{a['status']}">{STATUSES[a['status']]}</span></div>
        <div class="grid" style="grid-template-columns:repeat(3,1fr);margin-top:12px"><div><div class="small">Сумма</div><b>{a['amount']:,} ₽</b></div><div><div class="small">Срок</div><b>{a['term']} мес.</b></div><div><div class="small">Риск (демо)</div><b>{a['risk'] or '—'}</b></div></div>
        <p class="muted">{a['decision'] or 'Черновик сохранён. Отправьте заявку на рассмотрение.'}</p><a class="btn secondary" href="/application/{a['id']}">Открыть заявку →</a></div>"""
    body = f"""<section class="hero"><div class="eyebrow">Личный кабинет</div><h1>Добрый день!</h1><p>Здесь собраны заявки бизнеса. Следите за статусом и понимайте, что произойдёт дальше.</p><div class="actions"><a class="btn" href="/new">＋ Новая заявка</a></div></section><h2>Мои заявки <span class="pill">{len(rows)} всего</span></h2><div style="display:grid;gap:14px">{cards}</div>"""
    return page("Кабинет клиента", body, "customer")

def inn_valid(inn):
    digits=list(map(int,inn))
    if len(digits)==10:
        return sum(d*w for d,w in zip(digits[:9],[2,4,10,3,5,9,4,6,8]))%11%10==digits[9]
    if len(digits)==12:
        c11=sum(d*w for d,w in zip(digits[:10],[7,2,4,10,3,5,9,4,6,8]))%11%10
        c12=sum(d*w for d,w in zip(digits[:11],[3,7,2,4,10,3,5,9,4,6,8]))%11%10
        return c11==digits[10] and c12==digits[11]
    return False

@app.route("/new", methods=["GET", "POST"])
def new_application():
    if session.get("role") != "customer":
        return redirect(url_for("index"))

    if request.method == "POST":
        f = request.form
        try:
            org = f.get("org_form", "")
            inn = f.get("inn", "").strip()
            if org not in ("ИП", "ООО"):
                raise ValueError("Выберите организационную форму.")
            if not inn.isdigit() or len(inn) != (12 if org == "ИП" else 10) or not inn_valid(inn):
                raise ValueError("Проверьте ИНН: длина или контрольные цифры не совпадают.")

            required = ["company", "activity", "applicant", "role", "phone",
                        "email", "product", "purpose"]
            if any(not f.get(k, "").strip() for k in required):
                raise ValueError("Заполните все обязательные поля.")

            amount = int(f.get("amount", "0"))
            term = int(f.get("term", "0"))
            revenue = int(f.get("revenue", "0"))
            expenses = int(f.get("expenses", "0"))
            profit = int(f.get("profit", "0"))
            debt_yes = f.get("has_loans", "no") == "yes"
            debt = int(f.get("debt", "0")) if debt_yes else 0

            if amount <= 0 or term <= 0 or revenue <= 0:
                raise ValueError("Сумма, срок и выручка должны быть положительными.")
            if min(expenses, debt) < 0:
                raise ValueError("Расходы и долг не могут быть отрицательными.")

            reg_date = f.get("reg_date", "")
            if not reg_date or reg_date > datetime.now().date().isoformat():
                raise ValueError("Дата регистрации не может быть в будущем.")

            months = []
            for i in range(1, 13):
                val = f.get(f"month_{i}", "")
                year = f.get(f"year_{i}", "")
                if val == "" or not year:
                    raise ValueError("Укажите выручку и год для каждого из 12 месяцев.")
                if int(val) < 0 or int(year) < 2000 or int(year) > datetime.now().year:
                    raise ValueError("Проверьте месячную выручку и год.")
                months.append({"month": i, "year": int(year), "revenue": int(val)})

        except (ValueError, TypeError) as e:
            flash(str(e) or "Проверьте введённые данные.")
            return redirect(url_for("new_application"))

        validation = validate_company_data(inn, f["company"].strip(), f["product"])
        if not validation["success"]:
            flash(validation["error"])
            return redirect(url_for("new_application"))
        if validation.get("warning"):
            flash(validation["warning"])

        now = datetime.now().isoformat(timespec="minutes")
        extra = {k: f.get(k, "").strip() for k in
                 ("org_form", "activity", "reg_date", "applicant", "role",
                  "phone", "email", "profit", "has_loans")}
        extra["monthly_revenue"] = months
        extra["financial_warning"] = profit != revenue - expenses

        purpose = (f.get("other_purpose", "").strip()
                   if f.get("purpose") == "Другое" else f["purpose"])

        with connect() as con:
            cur = con.execute("""INSERT INTO applications(
                company, inn, contact, product, amount, term, purpose,
                revenue, expenses, debt, overdue, status, created_at, updated_at, extra_data)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f["company"].strip(), inn, f["applicant"].strip(), f["product"],
                 amount, term, purpose, revenue, expenses, debt, 0, "draft",
                 now, now, json.dumps(extra, ensure_ascii=False)))
            aid = cur.lastrowid

        created = get_app(aid)
        preliminary_score, preliminary_risk, score_details = calculate_score(created)
        saved_extra = json.loads(created["extra_data"] or "{}")
        saved_extra["score_details"] = score_details
        with connect() as con:
            con.execute("""UPDATE applications
                SET status='checking', score=?, risk=?, decision=?, extra_data=?, updated_at=?
                WHERE id=?""",
                (preliminary_score, preliminary_risk,
                 "Предварительный скоринг рассчитан. Автоматическая проверка не завершена: "
                 "официальные реестры и кредитная история не подключены; требуется проверка.",
                 json.dumps(saved_extra, ensure_ascii=False), now, aid))

        flash(f"Заявка №{aid} сохранена. Предварительная оценка рассчитана; "
              f"внешние проверки требуют подключения.")
        return redirect(url_for("application_detail", app_id=aid))

    body = r"""<section class="hero"><div class="eyebrow">Новая заявка · 3 шага</div><h1>Анкета для оформления бизнес-кредита</h1><p>Заполните данные бизнеса, параметры финансирования и финансовые показатели. Черновые данные сохраняются при переходе между шагами.</p><div class="steps"><span class="step active" data-step-label="0">01. Бизнес и заявитель</span><span class="step" data-step-label="1">02. Параметры кредита</span><span class="step" data-step-label="2">03. Финансы</span></div></section>
    <div class="card"><form id="loanForm" method="post">
    <section class="wizard-step" data-step="0"><h2>Данные бизнеса и заявителя</h2><div class="formgrid">
    <label>Организационная форма бизнеса *<select name="org_form" id="org_form" required><option value="">Выберите</option><option>ИП</option><option>ООО</option></select></label>
    <label>Название бизнеса *<input name="company" required placeholder="Название компании"></label>
    <label>ИНН *<input name="inn" id="inn" inputmode="numeric" pattern="[0-9]+" required placeholder="ИНН"><span class="field-error" id="innError"></span></label>
    <label>Сфера деятельности *<select name="activity" required><option value="">Выберите сферу</option><option>Торговля</option><option>Производство</option><option>Услуги</option><option>Строительство</option><option>IT и связь</option><option>Сельское хозяйство</option><option>Транспорт и логистика</option><option>Другое</option></select></label>
    <label>Дата регистрации *<input type="date" name="reg_date" id="reg_date" required></label>
    <label>ФИО заявителя *<input name="applicant" required placeholder="Иванов Иван Иванович"></label>
    <label>Роль заявителя *<select name="role" required><option value="">Выберите</option><option>Владелец</option><option>Руководитель</option><option>Представитель</option></select></label>
    <label>Номер телефона *<input name="phone" type="tel" required pattern="[+0-9() -]{10,20}" placeholder="+7 (999) 123-45-67"></label>
    <label class="full">Электронная почта *<input name="email" type="email" required placeholder="name@example.com"></label></div>
    <div class="actions"><button type="button" class="btn next">Продолжить →</button></div></section>
    <section class="wizard-step" data-step="1" hidden><h2>Параметры кредита</h2><div class="formgrid">
    <label>Кредитный продукт *<select name="product" required><option value="">Выберите продукт</option><option>Оборотный кредит</option><option>Кредит на оборудование</option><option>Кредит на развитие бизнеса</option></select></label>
    <label>Цель кредита *<select name="purpose" id="purpose" required><option value="">Выберите цель</option><option>Пополнение оборотных средств</option><option>Закупка сырья и товаров</option><option>Покупка оборудования</option><option>Расширение бизнеса</option><option>Рефинансирование</option><option>Другое</option></select></label>
    <label class="full" id="otherPurposeWrap" hidden>Уточните цель<input name="other_purpose" id="other_purpose" placeholder="Опишите цель кредита"></label>
    <label>Запрашиваемая сумма, ₽ *<input type="number" name="amount" min="1" step="1" required inputmode="numeric" placeholder="Например, 1200000"></label>
    <label>Срок кредита *<select name="term" required><option value="">Выберите срок</option><option value="6">6 месяцев</option><option value="12">12 месяцев</option><option value="18">18 месяцев</option><option value="24">24 месяца</option><option value="36">36 месяцев</option><option value="48">48 месяцев</option><option value="60">60 месяцев</option></select></label></div>
    <div class="actions"><button type="button" class="btn secondary prev">← Назад</button><button type="button" class="btn next">Продолжить →</button></div></section>
    <section class="wizard-step" data-step="2" hidden><h2>Финансовые показатели бизнеса</h2><div class="formgrid">
    <label>Средняя выручка в месяц, ₽ *<input type="number" name="revenue" min="1" step="1" required inputmode="numeric" placeholder="0"></label>
    <label>Средние расходы в месяц, ₽ *<input type="number" name="expenses" min="0" step="1" required inputmode="numeric" placeholder="0"></label>
    <label>Чистая прибыль в месяц, ₽ *<input type="number" name="profit" step="1" required inputmode="numeric" placeholder="Можно указать убыток"></label></div>
    <details style="margin-top:18px"><summary style="cursor:pointer;font-weight:700;margin-bottom:12px">Выручка за последние 12 месяцев — раскрыть</summary><p class="muted">Укажите сумму и год для каждого месяца.</p><div class="formgrid">"""
    months = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
              "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    for i, m in enumerate(months, 1):
        body += f'<label>{m}, выручка ₽ *<input type="number" name="month_{i}" min="0" step="1" required inputmode="numeric" placeholder="0"></label><label>Год для {m} *<select name="year_{i}" required><option value="">Год</option>'
        for year in range(datetime.now().year, max(1999, datetime.now().year - 3), -1):
            body += f'<option value="{year}">{year}</option>'
        body += '</select></label>'
    body += r"""</div></details><label>Есть ли у бизнеса действующие кредиты? *<select name="has_loans" id="has_loans" required><option value="">Выберите</option><option value="yes">Да</option><option value="no">Нет</option></select></label>
    <label id="debtWrap" hidden>Ежемесячные платежи по действующим кредитам, ₽ *<input type="number" name="debt" id="debt" min="0" step="1" inputmode="numeric" placeholder="0"></label>
    <div id="profitWarning" class="notice" hidden>Чистая прибыль не совпадает с разницей между выручкой и расходами. Проверьте данные, если это необходимо.</div>
    <div class="notice">После отправки запускается проверка. В этой демоверсии официальные источники и кредитная история не подключены, поэтому система не будет считать такую проверку успешно пройденной.</div>
    <div class="actions"><button type="button" class="btn secondary prev">← Назад</button><button type="submit" class="btn">Отправить заявку →</button></div></section></form></div>
    <script>
    (()=>{const form=document.getElementById('loanForm'),steps=[...document.querySelectorAll('.wizard-step')],labels=[...document.querySelectorAll('[data-step-label]')];let current=0;
    function show(n){current=n;steps.forEach((s,i)=>s.hidden=i!==n);labels.forEach((s,i)=>{s.classList.toggle('active',i===n);s.classList.toggle('done',i<n)});window.scrollTo({top:0,behavior:'smooth'});}
    function validStep(){const fields=[...steps[current].querySelectorAll('input,select')].filter(x=>!x.disabled&&!x.closest('[hidden]'));for(const f of fields){if(!f.checkValidity()){f.reportValidity();f.focus();return false;}}if(current===0){const type=document.getElementById('org_form').value,inn=document.getElementById('inn').value;const len=type==='ИП'?12:10;if(inn.length!==len){document.getElementById('inn').setCustomValidity('Для ИП нужно 12 цифр, для ООО — 10.');form.reportValidity();document.getElementById('inn').reportValidity();return false;}document.getElementById('inn').setCustomValidity('');}if(current===0){const d=document.getElementById('reg_date');if(d.value>new Date().toISOString().slice(0,10)){d.setCustomValidity('Дата регистрации не может быть в будущем.');d.reportValidity();return false;}d.setCustomValidity('');}return true;}
    document.querySelectorAll('.next').forEach(b=>b.addEventListener('click',()=>{if(validStep())show(Math.min(2,current+1))}));document.querySelectorAll('.prev').forEach(b=>b.addEventListener('click',()=>show(Math.max(0,current-1))));
    document.getElementById('purpose').addEventListener('change',e=>{const wrap=document.getElementById('otherPurposeWrap'),inp=document.getElementById('other_purpose');wrap.hidden=e.target.value!=='Другое';inp.required=!wrap.hidden;});
    document.getElementById('has_loans').addEventListener('change',e=>{const wrap=document.getElementById('debtWrap'),inp=document.getElementById('debt');wrap.hidden=e.target.value!=='yes';inp.required=!wrap.hidden;if(wrap.hidden)inp.value='';});
    ['revenue','expenses','profit'].forEach(id=>document.querySelector(`[name="${id}"]`).addEventListener('input',()=>{const r=Number(form.elements.revenue.value),e=Number(form.elements.expenses.value),p=Number(form.elements.profit.value);document.getElementById('profitWarning').hidden=!(form.elements.profit.value!==''&&p!==r-e)}));
    form.addEventListener('submit',e=>{if(!validStep()){e.preventDefault();return;}});
    })();
    </script>"""
    return page("Новая заявка", body, "customer")

@app.route("/application/<int:app_id>", methods=["GET", "POST"])
def application_detail(app_id):
    if session.get("role") != "customer":
        return redirect(url_for("index"))
    a = get_app(app_id)
    if not a:
        abort(404)

    if request.method == "POST":
        action = request.form.get("action")
        now = datetime.now().isoformat(timespec="minutes")
        with connect() as con:
            if action == "submit" and a["status"] == "draft":
                # скоринг уже рассчитан при создании заявки;
                # на submit только фиксируем переход в submitted
                con.execute(
                    "UPDATE applications SET status='submitted', decision=?, updated_at=? WHERE id=?",
                    ("Заявка отправлена. Следующий этап — проверка полноты и корректности данных.",
                     now, app_id))
                flash("Заявка отправлена на рассмотрение.")
            elif action == "sign" and a["status"] in ("approved", "offer"):
                con.execute(
                    "UPDATE applications SET status='signed', decision=?, updated_at=? WHERE id=?",
                    ("Документы условно оформлены в демонстрационном режиме.", now, app_id))
                flash("Демо-оформление завершено.")
            elif action == "fund" and a["status"] == "signed":
                con.execute(
                    "UPDATE applications SET status='funded', decision=?, updated_at=? WHERE id=?",
                    ("Выдача средств смоделирована; реальный перевод не выполняется.", now, app_id))
                flash("Демо-сценарий завершён.")
        return redirect(url_for("application_detail", app_id=app_id))

    a = get_app(app_id)
    score = a["score"] if a["score"] is not None else 0
    risk = a["risk"] or "Ещё не рассчитан"

    try:
        score_extra = json.loads(a["extra_data"] or "{}")
        score_details = score_extra.get("score_details", {})
    except (TypeError, ValueError):
        score_details = {}
    components_html = "".join(
        f'<tr><td>{item["name"]}<br><span class="small">{item["detail"]}</span></td>'
        f'<td><b>{item["points"]}/{item["max"]}</b></td></tr>'
        for item in score_details.get("components", []))

    flow = ["draft", "submitted", "checking", "review", "approved", "signed", "funded"]
    if a["status"] in flow:
        idx = flow.index(a["status"])
        steps = "".join(
            f'<span class="step {"done" if i < idx else "active" if i == idx else ""}">'
            f'{i + 1}. {STATUSES[s]}</span>'
            for i, s in enumerate(flow))
    else:
        steps = f'<span class="step active">{STATUSES[a["status"]]}</span>'

    button = (
        '<button class="btn" name="action" value="submit">Отправить на рассмотрение →</button>'
        if a["status"] == "draft" else
        '<button class="btn" name="action" value="sign">Оформить документы (демо)</button>'
        if a["status"] in ("approved", "offer") else
        '<button class="btn" name="action" value="fund">Смоделировать выдачу средств</button>'
        if a["status"] == "signed" else "")

    next_step = (
        "отправить заявку" if a["status"] == "draft" else
        "сотрудник банка проверяет данные и меняет статус"
        if a["status"] in ("submitted", "checking", "review") else
        "ознакомиться с решением и продолжить демо-сценарий"
        if a["status"] in ("approved", "offer") else
        "посмотреть комментарий банка и дальнейшие действия")

    body = f"""<div class="actions"><a class="btn secondary" href="/customer">← Все заявки</a></div><section class="hero"><div class="eyebrow">Заявка №{a['id']}</div><h1>{a['company']}</h1><p>{a['product']} · {a['purpose']}</p><div class="steps">{steps}</div></section>
    <div class="two"><div class="card"><h2>Параметры заявки</h2><table>
    <tr><th>ИНН</th><td>{a['inn']}</td></tr><tr><th>Контакт</th><td>{a['contact']}</td></tr><tr><th>Сумма</th><td><b>{a['amount']:,} ₽</b></td></tr><tr><th>Срок</th><td>{a['term']} месяцев</td></tr><tr><th>Выручка / мес.</th><td>{a['revenue']:,} ₽</td></tr><tr><th>Расходы / мес.</th><td>{a['expenses']:,} ₽</td></tr><tr><th>Обязательства</th><td>{a['debt']:,} ₽</td></tr><tr><th>Просрочка</th><td>{'Да' if a['overdue'] else 'Нет'}</td></tr><tr><th>Статус</th><td><span class="pill status-pill status-{a['status']}">{STATUSES[a['status']]}</span></td></tr></table><form method="post" style="margin-top:16px">{button}</form></div>
    <div class="card"><h2>Что происходит сейчас</h2><p>{a['decision'] or 'Черновик сохранён. Отправьте заявку, чтобы запустить процесс.'}</p><h3 style="margin-top:24px">Предварительный скоринг</h3><div style="display:flex;justify-content:space-between;margin-bottom:8px"><b>{score}/100</b><span class="pill {'green' if risk=='Низкий' else 'orange' if risk=='Средний' else 'redpill'}">{risk}</span></div><div class="riskbar"><span style="width:{score}%"></span></div><p class="small">Экспертная оценка по правилам прототипа, не вероятность дефолта и не решение банка.</p>{'<table><thead><tr><th>Фактор</th><th>Баллы</th></tr></thead><tbody>'+components_html+'</tbody></table>' if components_html else ''}<div class="notice">Ограничения: кредитная история и официальные реестры не подключены. Платёж по новому кредиту оценён как сумма / срок без процентов. Поэтому оценка предварительная и требует проверки.</div><div class="notice">Следующий шаг: {next_step}.</div></div></div>"""
    return page(f"Заявка №{app_id}", body, "customer")


@app.route("/bank")
def bank_dashboard():
    if session.get("role") != "bank": return redirect(url_for("index"))
    with connect() as con:
        rows = con.execute("""SELECT * FROM applications ORDER BY CASE status WHEN 'submitted' THEN 0 WHEN 'checking' THEN 1 WHEN 'review' THEN 2 ELSE 3 END, updated_at DESC""").fetchall()
    total=len(rows); active=sum(a["status"] in ("submitted","checking","review","more_info") for a in rows)
    body = f"""<section class="hero"><div class="eyebrow">Внутренний контур</div><h1>Очередь кредитных заявок</h1><p>Проверьте данные, оцените факторы риска и зафиксируйте следующее действие. Решения в прототипе демонстрационные.</p></section>
    <div class="grid" style="margin-bottom:20px"><div class="card"><div class="muted">Всего заявок</div><div class="metric">{total}</div><span class="small">Тестовая база</span></div><div class="card"><div class="muted">Активные</div><div class="metric">{active}</div><span class="small">Требуют внимания</span></div><div class="card"><div class="muted">Режим</div><div class="metric">Demo</div><span class="small">Без реальных интеграций</span></div></div>
    <div class="card"><h2>Все обращения</h2><div class="tablewrap"><table><thead><tr><th>Заявка / компания</th><th>Продукт</th><th>Сумма</th><th>Скоринг</th><th>Статус</th><th></th></tr></thead><tbody>"""
    for a in rows:
        body += f"""<tr><td><b>№{a['id']} · {a['company']}</b><br><span class="small">ИНН {a['inn']}</span></td><td>{a['product']}</td><td>{a['amount']:,} ₽</td><td>{a['score'] if a['score'] is not None else '—'} / 100<br><span class="small">{a['risk'] or '—'}</span></td><td><span class="pill status-pill status-{a['status']}">{STATUSES[a['status']]}</span></td><td><a href="/bank/application/{a['id']}">Открыть →</a></td></tr>"""
    body += "</tbody></table></div></div>"
    return page("Рабочее место банка", body, "bank")

@app.route("/bank/application/<int:app_id>", methods=["GET","POST"])
def bank_application(app_id):
    if session.get("role") != "bank": return redirect(url_for("index"))
    a = get_app(app_id)
    if not a: abort(404)
    if request.method == "POST":
        action=request.form.get("action")
        notes={
          "checking":"Демо-проверка данных завершена. Информация доступна для рассмотрения.",
          "review":"Заявка передана на рассмотрение сотруднику банка.",
          "more_info":"Нужны уточнения: проверьте финансовые показатели и дополните сведения.",
          "approved":"Демонстрационное одобрение. Для реального решения необходимы банковские проверки и полномочия.",
          "offer":"Предложены альтернативные условия финансирования для обсуждения.",
          "rejected":"Демонстрационный отказ по правилам прототипа; это не реальное решение банка."
        }
        if action in notes:
            now=datetime.now().isoformat(timespec="minutes")
            with connect() as con:
                con.execute("UPDATE applications SET status=?,decision=?,updated_at=? WHERE id=?",(action,notes[action],now,app_id))
            flash("Статус заявки обновлён.")
        return redirect(url_for("bank_application",app_id=app_id))

    a = get_app(app_id);
    score = a["score"] if a["score"] is not None else 0;
    risk = a["risk"] or "Не рассчитан"
    try:
        score_extra = json.loads(a["extra_data"] or "{}")
        score_details = score_extra.get("score_details", {})
    except (TypeError, ValueError):
        score_details = {}
    components_html = "".join(
        f'<tr><td>{item["name"]}<br><span class="small">{item["detail"]}</span></td><td><b>{item["points"]}/{item["max"]}</b></td></tr>'
        for item in score_details.get("components", []))
    body = f"""<div class="actions"><a class="btn secondary" href="/bank">← Очередь заявок</a></div><section class="hero"><div class="eyebrow">Рабочее место сотрудника · заявка №{a['id']}</div><h1>{a['company']}</h1><p>{a['product']} · {a['purpose']}</p><div class="actions"><span class="pill status-pill status-{a['status']}">{STATUSES[a['status']]}</span><span class="pill {'green' if risk == 'Низкий' else 'orange' if risk == 'Средний' else 'redpill'}">Риск: {risk}</span></div></section>
        <div class="two"><div class="card"><h2>Данные для рассмотрения</h2><table><tr><th>ИНН</th><td>{a['inn']}</td></tr><tr><th>Контакт</th><td>{a['contact']}</td></tr><tr><th>Сумма / срок</th><td>{a['amount']:,} ₽ / {a['term']} мес.</td></tr><tr><th>Выручка / мес.</th><td>{a['revenue']:,} ₽</td></tr><tr><th>Расходы / мес.</th><td>{a['expenses']:,} ₽</td></tr><tr><th>Маржа до иных платежей</th><td>{a['revenue'] - a['expenses']:,} ₽</td></tr><tr><th>Обязательства</th><td>{a['debt']:,} ₽</td></tr><tr><th>Просрочка</th><td>{'Есть' if a['overdue'] else 'Не указана'}</td></tr></table>
        <h3 style="margin-top:20px">Предварительный скоринг: {score}/100</h3><div class="riskbar"><span style="width:{score}%"></span></div><p class="small">Объяснимая экспертная оценка; не вероятность дефолта и не автоматическое решение.</p>{'<table><thead><tr><th>Фактор</th><th>Баллы</th></tr></thead><tbody>' + components_html + '</tbody></table>' if components_html else ''}<div class="notice">Кредитная история и официальные реестры не подключены. Оценка не может считаться финальной. Платёж по новому кредиту приближённый и не включает проценты.</div></div>
        <div class="card"><h2>Действие сотрудника</h2><p class="muted">Выберите следующий статус. Комментарий появится в кабинете предпринимателя.</p><form method="post">
        <button class="btn secondary" name="action" value="checking">✓ Проверка данных завершена</button><button class="btn secondary" name="action" value="review">Передать на рассмотрение</button><button class="btn secondary" name="action" value="more_info">Запросить уточнения</button><button class="btn" name="action" value="approved">Одобрить (демо)</button><button class="btn dark" name="action" value="offer">Другие условия</button><button class="btn secondary" name="action" value="rejected">Отказать (демо)</button></form>
        <div class="notice">Реальная система требует контроля доступа, аудита действий, банковских политик и проверки уполномоченным сотрудником.</div></div></div>"""
    return page(f"Рассмотрение №{app_id}", body, "bank")

@app.route("/health")
def health():
    return {"status": "ok", "service": "smart-credit-cabinet"}

@app.errorhandler(404)
def not_found(_):
    return page("Не найдено",'<div class="card"><h1>Страница не найдена</h1><a class="btn" href="/">На главную</a></div>',session.get("role")),404

def refresh_preliminary_scores():
    with connect() as con:
        rows = con.execute("SELECT * FROM applications").fetchall()
        for row in rows:
            score, risk, details = calculate_score(row)
            try:
                extra = json.loads(row["extra_data"] or "{}")
            except (TypeError, ValueError):
                extra = {}
            extra["score_details"] = details
            con.execute("UPDATE applications SET score=?,risk=?,extra_data=? WHERE id=?",
                        (score, risk, json.dumps(extra, ensure_ascii=False), row["id"]))

init_db()
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)