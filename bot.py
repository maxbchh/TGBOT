import asyncio
import logging
import os
import re
import hashlib
from pathlib import Path
from aiohttp import web
from decimal import Decimal, ROUND_HALF_UP

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, Update
)
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("Заполни BOT_TOKEN, SUPABASE_URL и SUPABASE_SERVICE_ROLE_KEY в .env")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("conductor_bot")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


class BuyTicket(StatesGroup):
    route = State()
    origin = State()
    destination = State()
    fare = State()
    payment = State()
    card_number = State()


def money(value):
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def db(query_fn):
    return await asyncio.to_thread(query_fn)


async def get_routes():
    def q():
        return supabase.table("conductor_routes").select(
            "id,name,city,stops,fare_per_stop"
        ).is_("archived_at", "null").order("name").execute().data
    return await db(q)


async def get_open_cash_shift():
    def q():
        rows = supabase.table("conductor_cash_shifts").select("id").eq(
            "status", "open"
        ).order("started_at", desc=True).limit(1).execute().data
        return rows[0] if rows else None
    return await db(q)


async def get_open_driver_shift(route_id=None):
    def q():
        query = supabase.table("conductor_shifts").select(
            "id,route_id,game_time,status"
        ).eq("status", "open").order("started_at", desc=True).limit(1)
        if route_id:
            query = query.eq("route_id", route_id)
        rows = query.execute().data
        return rows[0] if rows else None
    return await db(q)


def main_keyboard(user_id):
    rows = [
        [KeyboardButton(text="🎫 Купить билет")],
        [KeyboardButton(text="ℹ️ Мои возможности")],
        [KeyboardButton(text="🕒 Расписания")]
    ]
    if user_id in ADMIN_IDS:
        rows += [
            [KeyboardButton(text="🧾 Наличные заказы")],
            [KeyboardButton(text="🚌 Маршруты"), KeyboardButton(text="🚍 Подвижной состав")],
            [KeyboardButton(text="🧑‍✈️ Водительские смены"), KeyboardButton(text="💵 Кассовые смены")],
            [KeyboardButton(text="🎟 Последние билеты"), KeyboardButton(text="📊 Статистика")]
        ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def inline(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def stops_from_route(route):
    stops = route.get("stops") or []
    result = []
    for item in stops:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("title") or "").strip()
        else:
            name = str(item).strip()
        if name:
            result.append(name)
    return result




# Полные расписания из отдельного файла schedules.txt
SCHEDULE_FILE = Path(__file__).with_name("schedules.txt")


def load_schedules():
    result = {}
    try:
        lines = SCHEDULE_FILE.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        log.error("Файл schedules.txt не найден рядом с bot.py")
        return result
    except Exception:
        log.exception("Не удалось прочитать schedules.txt")
        return result

    route = None
    direction = None
    trip_lines = []

    def save_trip():
        if route and direction and trip_lines:
            result.setdefault(route, {}).setdefault(direction, []).append("\n".join(trip_lines).strip())

    for line in lines:
        header = re.match(r"^МАРШРУТ\s+(.+?)\s+—\s+(ТУДА|ОБРАТНО)\s*$", line.strip())
        if header:
            save_trip()
            route = header.group(1).strip()
            direction = header.group(2)
            trip_lines = []
            continue

        if line.strip().startswith("ОТПРАВЛЕНИЕ "):
            save_trip()
            trip_lines = [line.strip()]
            continue

        if trip_lines and line.strip() and not line.strip().startswith("===="):
            trip_lines.append(line.rstrip())

    save_trip()
    return result


SCHEDULES = load_schedules()
SCHEDULE_ROUTE_KEYS = sorted(
    SCHEDULES.keys(),
    key=lambda value: (0, int(value)) if value.isdigit() else (1, value.lower())
)


def schedule_directions(route):
    return [name for name in ("ТУДА", "ОБРАТНО") if name in SCHEDULES.get(route, {})]


def schedule_chunks(value, limit=3800):
    chunks = []
    current = ""
    for line in value.splitlines():
        candidate = (current + "\n" + line).strip() if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def show_schedule_routes(message):
    if not SCHEDULE_ROUTE_KEYS:
        await message.answer("Не удалось загрузить расписания. Проверь, что schedules.txt лежит рядом с bot.py.")
        return
    buttons = [
        [InlineKeyboardButton(text=f"Маршрут {route}", callback_data=f"schroute:{index}")]
        for index, route in enumerate(SCHEDULE_ROUTE_KEYS)
    ]
    await message.answer("🕒 Полные расписания\nВыбери маршрут:", reply_markup=inline(buttons))


@dp.message(F.text == "🕒 Расписания")
async def schedules_start(message: Message):
    await show_schedule_routes(message)


@dp.callback_query(F.data == "schlist")
async def schedules_list_callback(call: CallbackQuery):
    buttons = [
        [InlineKeyboardButton(text=f"Маршрут {route}", callback_data=f"schroute:{index}")]
        for index, route in enumerate(SCHEDULE_ROUTE_KEYS)
    ]
    await call.message.edit_text("🕒 Выбери маршрут:", reply_markup=inline(buttons))
    await call.answer()


@dp.callback_query(F.data.startswith("schroute:"))
async def schedule_choose_route(call: CallbackQuery):
    try:
        route_index = int(call.data.split(":")[1])
        route = SCHEDULE_ROUTE_KEYS[route_index]
    except (ValueError, IndexError):
        await call.answer("Маршрут не найден", show_alert=True)
        return
    directions = schedule_directions(route)
    buttons = [
        [InlineKeyboardButton(text=direction, callback_data=f"schdir:{route_index}:{i}")]
        for i, direction in enumerate(directions)
    ]
    buttons.append([InlineKeyboardButton(text="↩️ К маршрутам", callback_data="schlist")])
    await call.message.edit_text(f"Маршрут {route}. Выбери направление:", reply_markup=inline(buttons))
    await call.answer()


@dp.callback_query(F.data.startswith("schdir:"))
async def schedule_choose_direction(call: CallbackQuery):
    try:
        _, route_raw, direction_raw = call.data.split(":")
        route_index, direction_index = int(route_raw), int(direction_raw)
        route = SCHEDULE_ROUTE_KEYS[route_index]
        direction = schedule_directions(route)[direction_index]
        trips = SCHEDULES[route][direction]
    except (ValueError, IndexError, KeyError):
        await call.answer("Направление не найдено", show_alert=True)
        return
    buttons = [
        [InlineKeyboardButton(
            text=trip.splitlines()[0].replace("ОТПРАВЛЕНИЕ ", ""),
            callback_data=f"schtrip:{route_index}:{direction_index}:{i}"
        )]
        for i, trip in enumerate(trips)
    ]
    buttons.append([InlineKeyboardButton(text="↩️ К направлениям", callback_data=f"schroute:{route_index}")])
    await call.message.edit_text(
        f"Маршрут {route} — {direction}.\nВыбери время отправления:",
        reply_markup=inline(buttons)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("schtrip:"))
async def schedule_show_trip(call: CallbackQuery):
    try:
        _, route_raw, direction_raw, trip_raw = call.data.split(":")
        route_index, direction_index, trip_index = int(route_raw), int(direction_raw), int(trip_raw)
        route = SCHEDULE_ROUTE_KEYS[route_index]
        direction = schedule_directions(route)[direction_index]
        trip = SCHEDULES[route][direction][trip_index]
    except (ValueError, IndexError, KeyError):
        await call.answer("Рейс не найден", show_alert=True)
        return
    chunks = schedule_chunks(f"🚌 Маршрут {route} — {direction}\n\n{trip}")
    back = inline([[
        InlineKeyboardButton(text="↩️ К времени отправления", callback_data=f"schdir:{route_index}:{direction_index}")
    ]])
    await call.message.edit_text(chunks[0], reply_markup=back if len(chunks) == 1 else None)
    for chunk in chunks[1:]:
        await call.message.answer(chunk)
    if len(chunks) > 1:
        await call.message.answer("Навигация:", reply_markup=back)
    await call.answer()



@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Добро пожаловать в игровой бот «Кондуктор»!\n"
        "Здесь можно купить билет за виртуальные деньги.",
        reply_markup=main_keyboard(message.from_user.id)
    )


@dp.message(Command("myid"))
async def myid(message: Message):
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


@dp.message(F.text == "ℹ️ Мои возможности")
async def help_message(message: Message):
    await message.answer(
        "🎫 Покупка билета: выбери маршрут, остановки, тариф и способ оплаты.\n"
        "💳 Виртуальная карта: списание с карты из conductor_cards.\n"
        "💵 Наличные: заказ попадёт водителю/администратору на подтверждение.\n"
        "🕒 Расписания: просмотр полных расписаний по маршрутам и направлениям."
    )


@dp.message(F.text == "🎫 Купить билет")
async def buy_start(message: Message, state: FSMContext):
    await state.clear()
    try:
        routes = await get_routes()
    except Exception:
        log.exception("routes load failed")
        await message.answer("Не удалось загрузить маршруты из базы. Проверь подключение Supabase.")
        return
    if not routes:
        await message.answer("В базе пока нет доступных маршрутов.")
        return

    buttons = []
    for route in routes:
        stops = stops_from_route(route)
        if len(stops) >= 2:
            title = f"{route['name']} — {route.get('city') or 'город'}"
            buttons.append([InlineKeyboardButton(
                text=title[:60], callback_data=f"route:{route['id']}"
            )])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    await state.set_state(BuyTicket.route)
    await message.answer("Выбери маршрут:", reply_markup=inline(buttons))


@dp.callback_query(F.data.startswith("route:"))
async def choose_route(call: CallbackQuery, state: FSMContext):
    route_id = call.data.split(":", 1)[1]
    try:
        routes = await get_routes()
        route = next((r for r in routes if r["id"] == route_id), None)
    except Exception:
        route = None
    if not route:
        await call.answer("Маршрут не найден", show_alert=True)
        return
    stops = stops_from_route(route)
    await state.update_data(
        route_id=route["id"], route_name=route["name"],
        city=route.get("city", ""), stops=stops,
        fare_per_stop=str(route.get("fare_per_stop", "0.50"))
    )
    buttons = [
        [InlineKeyboardButton(text=f"{i+1}. {name}"[:60], callback_data=f"from:{i}")]
        for i, name in enumerate(stops)
    ]
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    await state.set_state(BuyTicket.origin)
    await call.message.edit_text(
        f"Маршрут {route['name']}.\nВыбери остановку отправления:",
        reply_markup=inline(buttons)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("from:"))
async def choose_origin(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = int(call.data.split(":")[1])
    stops = data.get("stops", [])
    if idx < 0 or idx >= len(stops) - 1:
        await call.answer("Для этой остановки нет дальнейшего участка.", show_alert=True)
        return
    await state.update_data(from_index=idx, from_stop=stops[idx])
    buttons = [
        [InlineKeyboardButton(text=f"{i+1}. {name}"[:60], callback_data=f"to:{i}")]
        for i, name in enumerate(stops[idx+1:], start=idx+1)
    ]
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="cancel")])
    await state.set_state(BuyTicket.destination)
    await call.message.edit_text(
        f"Откуда: {stops[idx]}\nТеперь выбери остановку назначения:",
        reply_markup=inline(buttons)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("to:"))
async def choose_destination(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    idx = int(call.data.split(":")[1])
    stops = data.get("stops", [])
    origin_idx = int(data.get("from_index", -1))
    if idx <= origin_idx or idx >= len(stops):
        await call.answer("Выбери остановку дальше по маршруту.", show_alert=True)
        return
    count = idx - origin_idx
    base = money(Decimal(data["fare_per_stop"]) * count)
    await state.update_data(to_index=idx, to_stop=stops[idx], stop_count=count,
                            base_amount=str(base))
    buttons = [
        [InlineKeyboardButton(text="Полный — 100%", callback_data="fare:Полный:1.00")],
        [InlineKeyboardButton(text="Льготный — 50%", callback_data="fare:Льготный:0.50")],
        [InlineKeyboardButton(text="Школьный — 75%", callback_data="fare:Школьный:0.75")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")]
    ]
    await state.set_state(BuyTicket.fare)
    await call.message.edit_text(
        f"Маршрут: {data['route_name']}\n"
        f"От: {data['from_stop']}\nДо: {stops[idx]}\n"
        f"Остановок: {count}\nБазовая стоимость: {base}",
        reply_markup=inline(buttons)
    )
    await call.answer()


@dp.callback_query(F.data.startswith("fare:"))
async def choose_fare(call: CallbackQuery, state: FSMContext):
    _, fare_type, multiplier = call.data.split(":")
    data = await state.get_data()
    amount = money(Decimal(data["base_amount"]) * Decimal(multiplier))
    await state.update_data(fare_type=fare_type, fare_multiplier=multiplier,
                            amount=str(amount))
    buttons = [
        [InlineKeyboardButton(text="💳 Виртуальная карта", callback_data="pay:card")],
        [InlineKeyboardButton(text="💵 Наличные водителю", callback_data="pay:cash")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")]
    ]
    await state.set_state(BuyTicket.payment)
    await call.message.edit_text(
        f"Тариф: {fare_type}\nК оплате: {amount}\nВыбери способ оплаты:",
        reply_markup=inline(buttons)
    )
    await call.answer()


def ticket_payload(data):
    return {
        "route_id": data["route_id"],
        "route_name": data["route_name"],
        "from_stop": data["from_stop"],
        "to_stop": data["to_stop"],
        "stop_count": int(data["stop_count"]),
        "fare_type": data["fare_type"],
        "fare_multiplier": str(data["fare_multiplier"]),
        "fare_per_stop": str(data["fare_per_stop"]),
        "amount": str(data["amount"]),
        "payment_method": "card",
        "cash_received": "0",
        "change_due": "0",
        "game_time": None,
        "card_id": None,
        "cash_shift_id": None,
        "shift_id": None,
    }


@dp.callback_query(F.data == "pay:card")
async def pay_card(call: CallbackQuery, state: FSMContext):
    await state.set_state(BuyTicket.card_number)
    await call.message.edit_text(
        "Введи номер своей виртуальной карты Conductor.\n"
        "Это номер игровой карты из вкладки банковских карт сайта.\n"
        "Не вводи данные настоящей банковской карты."
    )
    await call.answer()


@dp.message(BuyTicket.card_number)
async def card_number_received(message: Message, state: FSMContext):
    number = "".join(ch for ch in (message.text or "") if ch.isdigit())
    if len(number) < 8:
        await message.answer("Номер игровой карты выглядит слишком коротким. Введи его ещё раз.")
        return
    data = await state.get_data()
    payload = ticket_payload(data)
    payload["payment_method"] = "card"
    try:
        result = await db(lambda: supabase.rpc(
            "conductor_bot_pay_ticket",
            {
                "p_card_number": number,
                "p_ticket": payload,
                "p_telegram_user_id": str(message.from_user.id)
            }
        ).execute().data)
        row = result[0] if isinstance(result, list) and result else result
        if not row or not row.get("ticket_number"):
            await message.answer("Оплата не прошла. Проверь номер карты и её баланс.")
            return
        await message.answer(
            "✅ Билет оплачен и выпущен!\n"
            f"Билет № {row['ticket_number']}\n"
            f"Маршрут: {data['route_name']}\n"
            f"{data['from_stop']} → {data['to_stop']}\n"
            f"Тариф: {data['fare_type']}\n"
            f"Списано: {data['amount']}",
            reply_markup=main_keyboard(message.from_user.id)
        )
        await state.clear()
    except Exception:
        log.exception("card payment failed")
        await message.answer(
            "Не удалось провести оплату. Проверь, что SQL из файла "
            "conductor_bot_setup.sql уже выполнен в Supabase."
        )


@dp.callback_query(F.data == "pay:cash")
async def pay_cash(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    try:
        result = await db(lambda: supabase.rpc(
            "conductor_bot_create_cash_order",
            {
                "p_ticket": ticket_payload(data),
                "p_telegram_user_id": str(call.from_user.id),
                "p_username": call.from_user.username or "",
                "p_full_name": call.from_user.full_name or ""
            }
        ).execute().data)
        row = result[0] if isinstance(result, list) and result else result
        order_id = row.get("order_id") if row else None
        if not order_id:
            raise RuntimeError("No order id returned")
        await call.message.edit_text(
            "🕓 Заявка на билет создана!\n"
            f"Маршрут: {data['route_name']}\n"
            f"{data['from_stop']} → {data['to_stop']}\n"
            f"К оплате водителю: {data['amount']}\n\n"
            "Билет будет выпущен после подтверждения получения наличных водителем."
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    "💵 Новый заказ за наличные\n"
                    f"Заказ: {order_id}\nПассажир: {call.from_user.full_name}\n"
                    f"Telegram ID: {call.from_user.id}\n"
                    f"Маршрут: {data['route_name']}\n"
                    f"{data['from_stop']} → {data['to_stop']}\n"
                    f"Сумма: {data['amount']}",
                    reply_markup=inline([[
                        InlineKeyboardButton(
                            text="✅ Подтвердить оплату",
                            callback_data=f"cashok:{order_id}"
                        ),
                        InlineKeyboardButton(
                            text="❌ Отклонить",
                            callback_data=f"cashno:{order_id}"
                        )
                    ]])
                )
            except Exception:
                log.exception("notify admin failed")
        await state.clear()
    except Exception:
        log.exception("cash order failed")
        await call.message.edit_text(
            "Не удалось создать заказ. Убедись, что SQL из conductor_bot_setup.sql "
            "выполнен в Supabase."
        )
    await call.answer()


@dp.callback_query(F.data.startswith("cashok:"))
async def confirm_cash(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа", show_alert=True)
        return
    order_id = call.data.split(":", 1)[1]
    try:
        result = await db(lambda: supabase.rpc(
            "conductor_bot_confirm_cash_order",
            {"p_order_id": order_id, "p_admin_telegram_id": str(call.from_user.id)}
        ).execute().data)
        row = result[0] if isinstance(result, list) and result else result
        if not row or not row.get("ticket_number"):
            await call.answer("Заказ уже обработан или не найден.", show_alert=True)
            return
        await call.message.edit_text(
            call.message.text + f"\n\n✅ Подтверждено. Билет № {row['ticket_number']}"
        )
        try:
            await bot.send_message(
                int(row["telegram_user_id"]),
                f"✅ Водитель подтвердил оплату наличными. Билет № {row['ticket_number']} выпущен."
            )
        except Exception:
            pass
        await call.answer("Билет выпущен")
    except Exception:
        log.exception("cash confirm failed")
        await call.answer("Ошибка подтверждения. Проверь SQL-настройку.", show_alert=True)


@dp.callback_query(F.data.startswith("cashno:"))
async def reject_cash(call: CallbackQuery):
    if call.from_user.id not in ADMIN_IDS:
        await call.answer("Нет доступа", show_alert=True)
        return
    order_id = call.data.split(":", 1)[1]
    try:
        await db(lambda: supabase.rpc(
            "conductor_bot_reject_cash_order",
            {"p_order_id": order_id, "p_admin_telegram_id": str(call.from_user.id)}
        ).execute())
        await call.message.edit_text(call.message.text + "\n\n❌ Заказ отклонён.")
        await call.answer("Заказ отклонён")
    except Exception:
        log.exception("cash reject failed")
        await call.answer("Не удалось отклонить заказ.", show_alert=True)


@dp.callback_query(F.data == "cancel")
async def cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("Покупка отменена.")
    await call.answer()


def admin_only(message):
    return message.from_user.id in ADMIN_IDS


@dp.message(F.text == "🧾 Наличные заказы")
async def pending_orders(message: Message):
    if not admin_only(message):
        return
    try:
        rows = await db(lambda: supabase.table("conductor_ticket_orders").select(
            "id,telegram_user_id,passenger_name,route_name,from_stop,to_stop,amount,created_at"
        ).eq("status", "pending").order("created_at", desc=True).limit(15).execute().data)
        if not rows:
            await message.answer("Ожидающих наличных заказов нет.")
            return
        for r in rows:
            await message.answer(
                f"Заказ {r['id']}\nПассажир: {r.get('passenger_name') or r['telegram_user_id']}\n"
                f"{r['route_name']}: {r['from_stop']} → {r['to_stop']}\nСумма: {r['amount']}",
                reply_markup=inline([[
                    InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"cashok:{r['id']}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"cashno:{r['id']}")
                ]])
            )
    except Exception:
        log.exception("pending orders failed")
        await message.answer("Не удалось загрузить заказы. Выполни SQL-настройку.")


@dp.message(F.text == "🚌 Маршруты")
async def admin_routes(message: Message):
    if not admin_only(message):
        return
    try:
        rows = await get_routes()
        if not rows:
            await message.answer("Маршрутов нет.")
            return
        text = "\n".join(
            f"• {r['name']} ({len(stops_from_route(r))} ост.) — {r.get('city','')}"
            for r in rows[:40]
        )
        await message.answer("🚌 Маршруты:\n" + text)
    except Exception:
        log.exception("admin routes failed")
        await message.answer("Ошибка загрузки маршрутов.")


@dp.message(F.text == "🚍 Подвижной состав")
async def admin_vehicles(message: Message):
    if not admin_only(message):
        return
    try:
        rows = await db(lambda: supabase.table("conductor_vehicles").select(
            "number,model,city,status"
        ).is_("archived_at", "null").order("number").limit(50).execute().data)
        await message.answer(
            "🚍 Подвижной состав:\n" + ("\n".join(
                f"• №{r.get('number')} {r.get('model') or ''} — {r.get('status') or ''}"
                for r in rows
            ) if rows else "Записей нет.")
        )
    except Exception:
        log.exception("vehicles failed")
        await message.answer("Ошибка загрузки подвижного состава.")


@dp.message(F.text == "🧑‍✈️ Водительские смены")
async def admin_driver_shifts(message: Message):
    if not admin_only(message):
        return
    try:
        rows = await db(lambda: supabase.table("conductor_shifts").select(
            "id,route_id,status,game_time,started_at,ended_at"
        ).order("started_at", desc=True).limit(10).execute().data)
        await message.answer(
            "🧑‍✈️ Последние водительские смены:\n" + ("\n".join(
                f"• {r['id']} | маршрут {r.get('route_id') or '—'} | "
                f"{r.get('status')} | {r.get('game_time') or ''}"
                for r in rows
            ) if rows else "Смен нет.")
        )
    except Exception:
        log.exception("shifts failed")
        await message.answer("Ошибка загрузки смен.")


@dp.message(F.text == "💵 Кассовые смены")
async def admin_cash_shifts(message: Message):
    if not admin_only(message):
        return
    try:
        rows = await db(lambda: supabase.table("conductor_cash_shifts").select(
            "id,status,started_at,ended_at,opening_cash,closing_cash"
        ).order("started_at", desc=True).limit(10).execute().data)
        await message.answer(
            "💵 Последние кассовые смены:\n" + ("\n".join(
                f"• {r['id']} | {r['status']} | начало: {r.get('opening_cash')} | "
                f"закрытие: {r.get('closing_cash') or '—'}"
                for r in rows
            ) if rows else "Кассовых смен нет.")
        )
    except Exception:
        log.exception("cash shifts failed")
        await message.answer("Ошибка загрузки кассовых смен.")


@dp.message(F.text == "🎟 Последние билеты")
async def admin_tickets(message: Message):
    if not admin_only(message):
        return
    try:
        rows = await db(lambda: supabase.table("conductor_tickets").select(
            "ticket_number,route_name,from_stop,to_stop,amount,payment_method,fare_type,issued_at"
        ).is_("archived_at", "null").order("issued_at", desc=True).limit(10).execute().data)
        await message.answer(
            "🎟 Последние билеты:\n" + ("\n".join(
                f"№{r['ticket_number']} | {r['route_name']} | {r['from_stop']} → "
                f"{r['to_stop']} | {r['amount']} | {r['payment_method']} | {r['fare_type']}"
                for r in rows
            ) if rows else "Билетов нет.")
        )
    except Exception:
        log.exception("tickets failed")
        await message.answer("Ошибка загрузки билетов.")


@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if not admin_only(message):
        return
    try:
        tickets = await db(lambda: supabase.table("conductor_tickets").select(
            "ticket_number,amount,payment_method"
        ).is_("archived_at", "null").execute().data)
        total = len(tickets)
        revenue = sum((Decimal(str(x.get("amount") or 0)) for x in tickets), Decimal("0"))
        cash = sum(1 for x in tickets if x.get("payment_method") == "cash")
        card = sum(1 for x in tickets if x.get("payment_method") == "card")
        await message.answer(
            f"📊 Статистика по билетам\nВсего билетов: {total}\n"
            f"Сумма: {money(revenue)}\nНаличные: {cash}\nВиртуальная карта: {card}"
        )
    except Exception:
        log.exception("stats failed")
        await message.answer("Ошибка расчёта статистики.")


async def run_local_polling():
    log.info("Conductor bot started in polling mode")
    await dp.start_polling(bot)


async def health(request):
    return web.Response(text="Conductor bot is running")


async def telegram_webhook(request):
    expected_secret = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if supplied_secret != expected_secret:
        return web.Response(status=403, text="Forbidden")
    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("Webhook update processing failed")
        return web.Response(status=400, text="Bad Request")
    return web.Response(text="OK")


async def setup_webhook(app):
    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if not hostname:
        raise RuntimeError("RENDER_EXTERNAL_HOSTNAME is missing")
    webhook_url = f"https://{hostname}/telegram-webhook"
    secret = hashlib.sha256(BOT_TOKEN.encode("utf-8")).hexdigest()
    await bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        allowed_updates=dp.resolve_used_update_types()
    )
    log.info("Telegram webhook configured: %s", webhook_url)


async def close_bot_session(app):
    await bot.session.close()


def main():
    hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if not hostname:
        asyncio.run(run_local_polling())
        return
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_post("/telegram-webhook", telegram_webhook)
    app.on_startup.append(setup_webhook)
    app.on_cleanup.append(close_bot_session)
    port = int(os.getenv("PORT", "10000"))
    log.info("Starting Render webhook server on port %s", port)
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
