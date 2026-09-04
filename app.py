import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

import psycopg
from dotenv import load_dotenv
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")

if not app.config["SECRET_KEY"]:
    raise ValueError("SECRET_KEY belum ditemukan di file .env")


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise ValueError("DATABASE_URL belum ditemukan di file .env")

    return psycopg.connect(database_url)


def get_current_user():
    if "user_id" not in session:
        return None

    return {
        "id": session.get("user_id"),
        "full_name": session.get("full_name"),
        "username": session.get("username"),
        "role": session.get("role"),
    }


@app.context_processor
def inject_global_data():
    customer_cart = session.get("customer_cart", {})

    if not isinstance(customer_cart, dict):
        customer_cart = {}

    customer_cart_count = 0

    for quantity in customer_cart.values():
        try:
            customer_cart_count += int(quantity)
        except (TypeError, ValueError):
            continue

    return {
        "current_user": get_current_user(),
        "customer_cart_count": customer_cart_count,
    }


def login_required(view_function):
    @wraps(view_function)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            flash("Silakan login terlebih dahulu.", "error")
            return redirect(url_for("login"))

        return view_function(*args, **kwargs)

    return wrapped_view


def roles_required(*allowed_roles):
    def decorator(view_function):
        @wraps(view_function)
        def wrapped_view(*args, **kwargs):
            if "user_id" not in session:
                flash("Silakan login terlebih dahulu.", "error")
                return redirect(url_for("login"))

            if session.get("role") not in allowed_roles:
                flash("Anda tidak memiliki akses ke halaman ini.", "error")
                return redirect(url_for("redirect_by_role"))

            return view_function(*args, **kwargs)

        return wrapped_view

    return decorator


def redirect_user_by_role():
    if session.get("role") == "customer":
        return redirect(url_for("customer_shop"))

    return redirect(url_for("dashboard"))


def generate_category_code():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(MAX(id), 0) + 1
                FROM categories
            """)

            next_number = cur.fetchone()[0]

    return f"KTG-{next_number:03d}"


def generate_product_code():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(MAX(id), 0) + 1
                FROM products
            """)

            next_number = cur.fetchone()[0]

    return f"BRG-{next_number:03d}"


def generate_invoice_number(prefix="ORD"):
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    return f"{prefix}-{timestamp}"


def generate_payment_reference(payment_method):
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")

    if payment_method == "qris":
        return f"QRIS-{timestamp}"

    if payment_method == "ewallet":
        return f"EWALLET-{timestamp}"

    return f"PAY-{timestamp}"


def get_session_cart():
    cart = session.get("customer_cart", {})

    if not isinstance(cart, dict):
        return {}

    clean_cart = {}

    for product_id, quantity in cart.items():
        try:
            product_id_int = int(product_id)
            quantity_int = int(quantity)

            if product_id_int > 0 and quantity_int > 0:
                clean_cart[str(product_id_int)] = quantity_int

        except (TypeError, ValueError):
            continue

    return clean_cart


def save_session_cart(cart):
    session["customer_cart"] = cart
    session.modified = True


def get_cart_items():
    cart = get_session_cart()

    if not cart:
        return [], Decimal("0"), 0

    product_ids = [int(product_id) for product_id in cart.keys()]
    placeholders = ", ".join(["%s"] * len(product_ids))

    query = f"""
        SELECT
            p.id,
            p.product_code,
            p.product_name,
            p.selling_price,
            p.stock,
            p.unit,
            p.is_active,
            c.category_name
        FROM products p
        JOIN categories c
            ON c.id = p.category_id
        WHERE p.id IN ({placeholders})
        ORDER BY p.product_code ASC
    """

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, product_ids)
            product_rows = cur.fetchall()

    product_map = {
        row[0]: row
        for row in product_rows
    }

    cart_items = []
    total_amount = Decimal("0")
    total_items = 0

    for product_id_text, quantity in cart.items():
        product_id = int(product_id_text)
        product = product_map.get(product_id)

        if not product:
            continue

        subtotal = Decimal(product[3]) * quantity

        cart_items.append({
            "id": product[0],
            "product_code": product[1],
            "product_name": product[2],
            "selling_price": Decimal(product[3]),
            "stock": product[4],
            "unit": product[5],
            "is_active": product[6],
            "category_name": product[7],
            "quantity": quantity,
            "subtotal": subtotal,
        })

        total_amount += subtotal
        total_items += quantity

    return cart_items, total_amount, total_items


@app.route("/setup-admin", methods=["GET", "POST"])
def setup_admin():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*)
                    FROM app_users
                    WHERE role = 'admin'
                """)

                admin_count = cur.fetchone()[0]

        if admin_count > 0:
            flash("Akun admin sudah tersedia. Silakan login.", "error")
            return redirect(url_for("login"))

        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            if not full_name or not username or not password:
                flash("Nama lengkap, username, dan password wajib diisi.", "error")
                return redirect(url_for("setup_admin"))

            if len(username) < 3:
                flash("Username minimal 3 karakter.", "error")
                return redirect(url_for("setup_admin"))

            if len(password) < 6:
                flash("Password minimal 6 karakter.", "error")
                return redirect(url_for("setup_admin"))

            if password != confirm_password:
                flash("Konfirmasi password tidak sama.", "error")
                return redirect(url_for("setup_admin"))

            password_hash = generate_password_hash(password)

            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO app_users (
                            full_name,
                            username,
                            password_hash,
                            role,
                            is_active
                        )
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        full_name,
                        username,
                        password_hash,
                        "admin",
                        True,
                    ))

            flash("Admin pertama berhasil dibuat. Silakan login.", "success")
            return redirect(url_for("login"))

        return render_template("setup_admin.html")

    except psycopg.errors.UniqueViolation:
        flash("Username sudah digunakan.", "error")
        return redirect(url_for("setup_admin"))

    except Exception as error:
        return f"<h1>Setup admin gagal ❌</h1><pre>{error}</pre>", 500


@app.route("/setup-kasir")
@roles_required("admin")
def setup_kasir():
    full_name = "Kasir Sahabat Desa"
    username = "kasir1"
    password = "123456"

    try:
        password_hash = generate_password_hash(password)

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO app_users (
                        full_name,
                        username,
                        password_hash,
                        role,
                        is_active
                    )
                    VALUES (%s, %s, %s, %s, %s)

                    ON CONFLICT (username)
                    DO UPDATE SET
                        full_name = EXCLUDED.full_name,
                        password_hash = EXCLUDED.password_hash,
                        role = EXCLUDED.role,
                        is_active = EXCLUDED.is_active
                """, (
                    full_name,
                    username,
                    password_hash,
                    "kasir",
                    True,
                ))

        flash(
            "Akun kasir berhasil dibuat atau di-reset. "
            "Username: kasir1 | Password: 123456",
            "success"
        )

    except Exception as error:
        flash(f"Gagal membuat atau reset akun kasir: {error}", "error")

    return redirect(url_for("dashboard"))


@app.route("/", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("redirect_by_role"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Username dan password wajib diisi.", "error")
            return redirect(url_for("login"))

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT
                            id,
                            full_name,
                            username,
                            password_hash,
                            role,
                            is_active
                        FROM app_users
                        WHERE username = %s
                    """, (username,))

                    user = cur.fetchone()

            if not user:
                flash("Username atau password salah.", "error")
                return redirect(url_for("login"))

            user_id, full_name, username_db, password_hash, role, is_active = user

            if not is_active:
                flash("Akun Anda sedang tidak aktif.", "error")
                return redirect(url_for("login"))

            if not check_password_hash(password_hash, password):
                flash("Username atau password salah.", "error")
                return redirect(url_for("login"))

            session.clear()

            session["user_id"] = user_id
            session["full_name"] = full_name
            session["username"] = username_db
            session["role"] = role

            flash(f"Selamat datang, {full_name}.", "success")
            return redirect(url_for("redirect_by_role"))

        except Exception as error:
            return f"<h1>Login gagal ❌</h1><pre>{error}</pre>", 500

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_id" in session:
        return redirect(url_for("redirect_by_role"))

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not full_name or not username or not password:
            flash("Nama lengkap, username, dan password wajib diisi.", "error")
            return redirect(url_for("register"))

        if len(username) < 3:
            flash("Username minimal 3 karakter.", "error")
            return redirect(url_for("register"))

        if len(password) < 6:
            flash("Password minimal 6 karakter.", "error")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("Konfirmasi password tidak sama.", "error")
            return redirect(url_for("register"))

        try:
            password_hash = generate_password_hash(password)

            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO app_users (
                            full_name,
                            username,
                            password_hash,
                            role,
                            is_active
                        )
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        full_name,
                        username,
                        password_hash,
                        "customer",
                        True,
                    ))

            flash("Registrasi berhasil. Silakan login untuk belanja.", "success")
            return redirect(url_for("login"))

        except psycopg.errors.UniqueViolation:
            flash("Username sudah digunakan.", "error")
            return redirect(url_for("register"))

        except Exception as error:
            return f"<h1>Registrasi gagal ❌</h1><pre>{error}</pre>", 500

    return render_template("register.html")


@app.route("/logout")
@login_required
def logout():
    session.clear()
    flash("Anda berhasil logout.", "success")
    return redirect(url_for("login"))


@app.route("/redirect-by-role")
@login_required
def redirect_by_role():
    return redirect_user_by_role()


@app.route("/dashboard")
@roles_required("admin", "kasir")
def dashboard():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*)
                    FROM categories
                    WHERE is_active = true
                """)
                total_categories = cur.fetchone()[0]

                cur.execute("""
                    SELECT COUNT(*)
                    FROM products
                    WHERE is_active = true
                """)
                total_products = cur.fetchone()[0]

                cur.execute("""
                    SELECT COUNT(*)
                    FROM sales
                    WHERE DATE(transaction_date) = CURRENT_DATE
                    AND status IN ('confirmed', 'completed')
                """)
                total_transactions_today = cur.fetchone()[0]

                cur.execute("""
                    SELECT COALESCE(SUM(subtotal), 0)
                    FROM sales
                    WHERE DATE(transaction_date) = CURRENT_DATE
                    AND status IN ('confirmed', 'completed')
                """)
                omzet_today = cur.fetchone()[0]

                cur.execute("""
                    SELECT COUNT(*)
                    FROM products
                    WHERE is_active = true
                    AND stock <= min_stock
                """)
                low_stock_products = cur.fetchone()[0]

        return render_template(
            "dashboard.html",
            total_categories=total_categories,
            total_products=total_products,
            total_transactions_today=total_transactions_today,
            omzet_today=omzet_today,
            low_stock_products=low_stock_products,
        )

    except Exception as error:
        return f"<h1>Dashboard gagal dimuat ❌</h1><pre>{error}</pre>", 500


@app.route("/shop")
@roles_required("customer")
def customer_shop():
    search = request.args.get("search", "").strip()

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        p.id,
                        p.product_code,
                        p.product_name,
                        c.category_name,
                        p.selling_price,
                        p.stock,
                        p.unit
                    FROM products p
                    JOIN categories c
                        ON c.id = p.category_id
                    WHERE p.is_active = true
                    AND p.stock > 0
                    AND (
                        %s = ''
                        OR p.product_code ILIKE %s
                        OR p.product_name ILIKE %s
                        OR c.category_name ILIKE %s
                    )
                    ORDER BY p.product_code ASC
                """, (
                    search,
                    f"%{search}%",
                    f"%{search}%",
                    f"%{search}%",
                ))

                products = cur.fetchall()

        return render_template(
            "customer_shop.html",
            products=products,
            search=search,
        )

    except Exception as error:
        return f"<h1>Halaman toko gagal dimuat ❌</h1><pre>{error}</pre>", 500


@app.route("/shop/cart/add/<int:product_id>", methods=["POST"])
@roles_required("customer")
def customer_cart_add(product_id):
    quantity_raw = request.form.get("quantity", "1").strip()

    try:
        quantity = int(quantity_raw)

        if quantity <= 0:
            raise ValueError

    except ValueError:
        flash("Jumlah produk harus lebih dari nol.", "error")
        return redirect(url_for("customer_shop"))

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, product_name, stock, is_active
                    FROM products
                    WHERE id = %s
                """, (product_id,))

                product = cur.fetchone()

        if not product:
            flash("Produk tidak ditemukan.", "error")
            return redirect(url_for("customer_shop"))

        product_id_db, product_name, stock, is_active = product

        if not is_active or stock <= 0:
            flash("Produk sedang tidak tersedia.", "error")
            return redirect(url_for("customer_shop"))

        cart = get_session_cart()
        current_quantity = cart.get(str(product_id_db), 0)

        if current_quantity + quantity > stock:
            flash(
                f"Stok {product_name} tidak cukup. Stok tersedia: {stock}.",
                "error"
            )
            return redirect(url_for("customer_shop"))

        cart[str(product_id_db)] = current_quantity + quantity
        save_session_cart(cart)

        flash(f"{product_name} masuk ke keranjang.", "success")

    except Exception as error:
        flash(f"Gagal menambah produk ke keranjang: {error}", "error")

    return redirect(url_for("customer_shop"))


@app.route("/shop/cart")
@roles_required("customer")
def customer_cart():
    cart_items, total_amount, total_items = get_cart_items()

    return render_template(
        "customer_cart.html",
        cart_items=cart_items,
        total_amount=total_amount,
        total_items=total_items,
    )


@app.route("/shop/cart/update", methods=["POST"])
@roles_required("customer")
def customer_cart_update():
    product_id_raw = request.form.get("product_id", "").strip()
    quantity_raw = request.form.get("quantity", "").strip()

    try:
        product_id = int(product_id_raw)
        quantity = int(quantity_raw)

        cart = get_session_cart()

        if str(product_id) not in cart:
            flash("Produk tidak ada di keranjang.", "error")
            return redirect(url_for("customer_cart"))

        if quantity <= 0:
            cart.pop(str(product_id), None)
            save_session_cart(cart)

            flash("Produk dihapus dari keranjang.", "success")
            return redirect(url_for("customer_cart"))

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT product_name, stock
                    FROM products
                    WHERE id = %s
                """, (product_id,))

                product = cur.fetchone()

        if not product:
            cart.pop(str(product_id), None)
            save_session_cart(cart)

            flash("Produk tidak ditemukan dan dihapus dari keranjang.", "error")
            return redirect(url_for("customer_cart"))

        product_name, stock = product

        if quantity > stock:
            flash(
                f"Stok {product_name} tidak cukup. Stok tersedia: {stock}.",
                "error"
            )
            return redirect(url_for("customer_cart"))

        cart[str(product_id)] = quantity
        save_session_cart(cart)

        flash("Jumlah produk di keranjang diperbarui.", "success")

    except (TypeError, ValueError):
        flash("Jumlah produk tidak valid.", "error")

    return redirect(url_for("customer_cart"))


@app.route("/shop/cart/remove/<int:product_id>", methods=["POST"])
@roles_required("customer")
def customer_cart_remove(product_id):
    cart = get_session_cart()
    cart.pop(str(product_id), None)
    save_session_cart(cart)

    flash("Produk berhasil dihapus dari keranjang.", "success")
    return redirect(url_for("customer_cart"))


@app.route("/shop/checkout", methods=["POST"])
@roles_required("customer")
def customer_checkout():
    notes = request.form.get("notes", "").strip()
    payment_method = request.form.get("payment_method", "").strip()
    cart = get_session_cart()

    allowed_payment_methods = ("cash", "qris", "ewallet")

    if payment_method not in allowed_payment_methods:
        flash("Silakan pilih metode pembayaran.", "error")
        return redirect(url_for("customer_cart"))

    if not cart:
        flash("Keranjang Anda masih kosong.", "error")
        return redirect(url_for("customer_cart"))

    try:
        product_ids = [int(product_id) for product_id in cart.keys()]
        placeholders = ", ".join(["%s"] * len(product_ids))

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                query = f"""
                    SELECT
                        id,
                        product_code,
                        product_name,
                        selling_price,
                        stock,
                        is_active
                    FROM products
                    WHERE id IN ({placeholders})
                    FOR UPDATE
                """

                cur.execute(query, product_ids)
                product_rows = cur.fetchall()

                product_map = {
                    row[0]: row
                    for row in product_rows
                }

                sale_items = []
                total_amount = Decimal("0")
                total_items = 0

                for product_id_text, quantity in cart.items():
                    product_id = int(product_id_text)
                    product = product_map.get(product_id)

                    if not product:
                        raise ValueError(
                            "Ada produk di keranjang yang tidak ditemukan."
                        )

                    (
                        product_id_db,
                        product_code,
                        product_name,
                        selling_price,
                        stock,
                        is_active,
                    ) = product

                    if not is_active:
                        raise ValueError(
                            f"Produk {product_name} sedang tidak aktif."
                        )

                    if quantity > stock:
                        raise ValueError(
                            f"Stok {product_name} tidak cukup. "
                            f"Stok tersedia: {stock}."
                        )

                    subtotal = Decimal(selling_price) * quantity

                    sale_items.append({
                        "product_id": product_id_db,
                        "product_code": product_code,
                        "product_name": product_name,
                        "selling_price": Decimal(selling_price),
                        "quantity": quantity,
                        "subtotal": subtotal,
                    })

                    total_amount += subtotal
                    total_items += quantity

                invoice_number = generate_invoice_number("ORD")
                is_online_payment = payment_method in ("qris", "ewallet")

                if is_online_payment:
                    order_status = "confirmed"
                    payment_reference = generate_payment_reference(payment_method)
                    paid_at_value = datetime.now()
                else:
                    order_status = "pending"
                    payment_reference = None
                    paid_at_value = None

                cur.execute("""
                    INSERT INTO sales (
                        invoice_number,
                        transaction_date,
                        cashier_id,
                        customer_id,
                        total_items,
                        subtotal,
                        cash_amount,
                        change_amount,
                        notes,
                        status,
                        payment_method,
                        payment_reference,
                        paid_at,
                        handover_at
                    )
                    VALUES (
                        %s, NOW(), NULL, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, NULL
                    )
                    RETURNING id
                """, (
                    invoice_number,
                    session["user_id"],
                    total_items,
                    total_amount,
                    total_amount if is_online_payment else Decimal("0"),
                    Decimal("0"),
                    notes if notes else None,
                    order_status,
                    payment_method,
                    payment_reference,
                    paid_at_value,
                ))

                sale_id = cur.fetchone()[0]

                for item in sale_items:
                    cur.execute("""
                        INSERT INTO sale_items (
                            sale_id,
                            product_id,
                            product_code,
                            product_name,
                            selling_price,
                            quantity,
                            subtotal
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        sale_id,
                        item["product_id"],
                        item["product_code"],
                        item["product_name"],
                        item["selling_price"],
                        item["quantity"],
                        item["subtotal"],
                    ))

                if is_online_payment:
                    for item in sale_items:
                        cur.execute("""
                            UPDATE products
                            SET stock = stock - %s
                            WHERE id = %s
                        """, (
                            item["quantity"],
                            item["product_id"],
                        ))

        save_session_cart({})

        if payment_method == "cash":
            flash(
                f"Pesanan {invoice_number} berhasil dibuat. "
                "Silakan lakukan pembayaran kepada kasir.",
                "success"
            )
        else:
            flash(
                f"Pembayaran online simulasi berhasil. "
                f"Pesanan {invoice_number} sudah dibayar dan siap diambil.",
                "success"
            )

        return redirect(url_for("customer_orders"))

    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("customer_cart"))

    except Exception as error:
        flash(f"Gagal membuat pesanan: {error}", "error")
        return redirect(url_for("customer_cart"))


@app.route("/shop/orders")
@roles_required("customer")
def customer_orders():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id,
                        invoice_number,
                        transaction_date,
                        total_items,
                        subtotal,
                        status,
                        notes,
                        payment_method,
                        payment_reference,
                        paid_at,
                        handover_at
                    FROM sales
                    WHERE customer_id = %s
                    ORDER BY transaction_date ASC
                """, (session["user_id"],))

                orders = cur.fetchall()

        return render_template(
            "customer_orders.html",
            orders=orders,
        )

    except Exception as error:
        return f"<h1>Pesanan gagal dimuat ❌</h1><pre>{error}</pre>", 500


@app.route("/shop/orders/<int:sale_id>/cancel", methods=["POST"])
@roles_required("customer")
def customer_cancel_order(sale_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        invoice_number,
                        status,
                        payment_method
                    FROM sales
                    WHERE id = %s
                    AND customer_id = %s
                    FOR UPDATE
                """, (sale_id, session["user_id"]))

                order = cur.fetchone()

                if not order:
                    flash("Pesanan tidak ditemukan.", "error")
                    return redirect(url_for("customer_orders"))

                invoice_number, status, payment_method = order

                if status != "pending" or payment_method != "cash":
                    flash(
                        "Hanya pesanan tunai yang belum dibayar dapat dibatalkan.",
                        "error"
                    )
                    return redirect(url_for("customer_orders"))

                cur.execute("""
                    UPDATE sales
                    SET status = 'cancelled'
                    WHERE id = %s
                """, (sale_id,))

        flash(
            f"Pesanan {invoice_number} berhasil dibatalkan.",
            "success"
        )

    except Exception as error:
        flash(f"Gagal membatalkan pesanan: {error}", "error")

    return redirect(url_for("customer_orders"))


@app.route("/orders")
@roles_required("kasir")
def staff_orders():
    status_filter = request.args.get("status", "").strip()
    keyword = request.args.get("keyword", "").strip()

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        s.id,
                        s.invoice_number,
                        s.transaction_date,
                        customer.full_name,
                        customer.username,
                        s.total_items,
                        s.subtotal,
                        s.cash_amount,
                        s.change_amount,
                        s.status,
                        s.notes,
                        COALESCE(cashier.full_name, '-'),
                        s.payment_method,
                        s.payment_reference,
                        s.paid_at,
                        s.handover_at
                    FROM sales s
                    JOIN app_users customer
                        ON customer.id = s.customer_id
                    LEFT JOIN app_users cashier
                        ON cashier.id = s.cashier_id
                    WHERE s.customer_id IS NOT NULL
                    AND (
                        %s = ''
                        OR s.status = %s
                    )
                    AND (
                        %s = ''
                        OR s.invoice_number ILIKE %s
                        OR customer.full_name ILIKE %s
                        OR customer.username ILIKE %s
                    )
                    ORDER BY s.transaction_date ASC
                """, (
                    status_filter,
                    status_filter,
                    keyword,
                    f"%{keyword}%",
                    f"%{keyword}%",
                    f"%{keyword}%",
                ))

                orders = cur.fetchall()

        return render_template(
            "staff_orders.html",
            orders=orders,
            status_filter=status_filter,
            keyword=keyword,
        )

    except Exception as error:
        return f"<h1>Pesanan gagal dimuat ❌</h1><pre>{error}</pre>", 500


@app.route("/orders/<int:sale_id>/process-cash-payment", methods=["POST"])
@roles_required("kasir")
def process_cash_payment(sale_id):
    cash_amount_raw = request.form.get("cash_amount", "").strip()

    try:
        cash_amount = Decimal(cash_amount_raw)

        if cash_amount < 0:
            raise ValueError

    except (InvalidOperation, ValueError):
        flash("Nominal uang bayar tidak valid.", "error")
        return redirect(url_for("staff_orders"))

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id,
                        invoice_number,
                        subtotal,
                        status,
                        payment_method
                    FROM sales
                    WHERE id = %s
                    AND customer_id IS NOT NULL
                    FOR UPDATE
                """, (sale_id,))

                sale = cur.fetchone()

                if not sale:
                    flash("Pesanan customer tidak ditemukan.", "error")
                    return redirect(url_for("staff_orders"))

                sale_id_db, invoice_number, subtotal, status, payment_method = sale
                subtotal = Decimal(subtotal)

                if payment_method != "cash":
                    flash(
                        "Pesanan ini bukan pembayaran tunai di kasir.",
                        "error"
                    )
                    return redirect(url_for("staff_orders"))

                if status != "pending":
                    flash(
                        "Pesanan ini sudah diproses sebelumnya.",
                        "error"
                    )
                    return redirect(url_for("staff_orders"))

                if cash_amount < subtotal:
                    total_text = f"{subtotal:,.0f}".replace(",", ".")

                    flash(
                        f"Uang bayar kurang. Total belanja Rp{total_text}.",
                        "error"
                    )
                    return redirect(url_for("staff_orders"))

                cur.execute("""
                    SELECT
                        product_id,
                        product_name,
                        quantity
                    FROM sale_items
                    WHERE sale_id = %s
                """, (sale_id_db,))

                sale_items = cur.fetchall()
                stock_problems = []

                for product_id, product_name, quantity in sale_items:
                    cur.execute("""
                        SELECT
                            stock,
                            is_active
                        FROM products
                        WHERE id = %s
                        FOR UPDATE
                    """, (product_id,))

                    product = cur.fetchone()

                    if not product:
                        stock_problems.append(
                            f"Produk {product_name} tidak ditemukan."
                        )
                        continue

                    stock, is_active = product

                    if not is_active:
                        stock_problems.append(
                            f"Produk {product_name} sedang nonaktif."
                        )
                        continue

                    if quantity > stock:
                        stock_problems.append(
                            f"Stok {product_name} kurang. "
                            f"Tersedia {stock}, dipesan {quantity}."
                        )

                if stock_problems:
                    flash(" | ".join(stock_problems), "error")
                    return redirect(url_for("staff_orders"))

                for product_id, product_name, quantity in sale_items:
                    cur.execute("""
                        UPDATE products
                        SET stock = stock - %s
                        WHERE id = %s
                    """, (quantity, product_id))

                change_amount = cash_amount - subtotal

                cur.execute("""
                    UPDATE sales
                    SET
                        status = 'completed',
                        cashier_id = %s,
                        cash_amount = %s,
                        change_amount = %s,
                        paid_at = NOW(),
                        handover_at = NOW()
                    WHERE id = %s
                """, (
                    session["user_id"],
                    cash_amount,
                    change_amount,
                    sale_id_db,
                ))

        flash(
            f"Pembayaran tunai pesanan {invoice_number} berhasil diproses.",
            "success"
        )

        return redirect(url_for("sale_receipt", sale_id=sale_id))

    except Exception as error:
        flash(f"Gagal memproses pembayaran tunai: {error}", "error")
        return redirect(url_for("staff_orders"))


@app.route("/orders/<int:sale_id>/handover", methods=["POST"])
@roles_required("kasir")
def handover_online_order(sale_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        invoice_number,
                        status,
                        payment_method
                    FROM sales
                    WHERE id = %s
                    AND customer_id IS NOT NULL
                    FOR UPDATE
                """, (sale_id,))

                sale = cur.fetchone()

                if not sale:
                    flash("Pesanan tidak ditemukan.", "error")
                    return redirect(url_for("staff_orders"))

                invoice_number, status, payment_method = sale

                if payment_method not in ("qris", "ewallet"):
                    flash(
                        "Menu ini hanya untuk pesanan online yang sudah dibayar.",
                        "error"
                    )
                    return redirect(url_for("staff_orders"))

                if status != "confirmed":
                    flash(
                        "Pesanan ini belum siap diserahkan atau sudah selesai.",
                        "error"
                    )
                    return redirect(url_for("staff_orders"))

                cur.execute("""
                    UPDATE sales
                    SET
                        status = 'completed',
                        cashier_id = %s,
                        handover_at = NOW()
                    WHERE id = %s
                """, (
                    session["user_id"],
                    sale_id,
                ))

        flash(
            f"Barang untuk pesanan {invoice_number} berhasil diserahkan.",
            "success"
        )

        return redirect(url_for("sale_receipt", sale_id=sale_id))

    except Exception as error:
        flash(f"Gagal menyerahkan pesanan: {error}", "error")
        return redirect(url_for("staff_orders"))


@app.route("/categories")
@roles_required("admin")
def categories():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id,
                        category_code,
                        category_name,
                        description,
                        is_active
                    FROM categories
                    ORDER BY category_code ASC
                """)

                category_rows = cur.fetchall()

        return render_template(
            "categories.html",
            categories=category_rows,
        )

    except Exception as error:
        return f"<h1>Data kategori gagal dimuat ❌</h1><pre>{error}</pre>", 500


@app.route("/categories/add", methods=["POST"])
@roles_required("admin")
def add_category():
    category_name = request.form.get("category_name", "").strip()
    description = request.form.get("description", "").strip()
    is_active = request.form.get("is_active") == "on"

    if not category_name:
        flash("Nama kategori wajib diisi.", "error")
        return redirect(url_for("categories"))

    try:
        category_code = generate_category_code()

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO categories (
                        category_code,
                        category_name,
                        description,
                        is_active
                    )
                    VALUES (%s, %s, %s, %s)
                """, (
                    category_code,
                    category_name,
                    description if description else None,
                    is_active,
                ))

        flash("Kategori barang berhasil ditambahkan.", "success")

    except psycopg.errors.UniqueViolation:
        flash("Nama kategori sudah tersedia.", "error")

    except Exception as error:
        flash(f"Gagal menambahkan kategori: {error}", "error")

    return redirect(url_for("categories"))


@app.route("/categories/<int:category_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def edit_category(category_id):
    try:
        if request.method == "POST":
            category_name = request.form.get("category_name", "").strip()
            description = request.form.get("description", "").strip()
            is_active = request.form.get("is_active") == "on"

            if not category_name:
                flash("Nama kategori wajib diisi.", "error")
                return redirect(
                    url_for("edit_category", category_id=category_id)
                )

            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE categories
                        SET
                            category_name = %s,
                            description = %s,
                            is_active = %s
                        WHERE id = %s
                    """, (
                        category_name,
                        description if description else None,
                        is_active,
                        category_id,
                    ))

            flash("Kategori berhasil diubah.", "success")
            return redirect(url_for("categories"))

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id,
                        category_code,
                        category_name,
                        description,
                        is_active
                    FROM categories
                    WHERE id = %s
                """, (category_id,))

                category = cur.fetchone()

        if not category:
            flash("Data kategori tidak ditemukan.", "error")
            return redirect(url_for("categories"))

        return render_template(
            "edit_category.html",
            category=category,
        )

    except psycopg.errors.UniqueViolation:
        flash("Nama kategori sudah digunakan.", "error")
        return redirect(
            url_for("edit_category", category_id=category_id)
        )

    except Exception as error:
        return f"<h1>Kategori gagal diproses ❌</h1><pre>{error}</pre>", 500


@app.route("/categories/<int:category_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_category(category_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM categories
                    WHERE id = %s
                """, (category_id,))

        flash("Kategori berhasil dihapus.", "success")

    except psycopg.errors.ForeignKeyViolation:
        flash(
            "Kategori tidak bisa dihapus karena sudah dipakai produk.",
            "error"
        )

    except Exception as error:
        flash(f"Gagal menghapus kategori: {error}", "error")

    return redirect(url_for("categories"))


@app.route("/products")
@roles_required("admin", "kasir")
def products():
    search = request.args.get("search", "").strip()

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        p.id,
                        p.product_code,
                        p.product_name,
                        c.category_name,
                        p.purchase_price,
                        p.selling_price,
                        p.stock,
                        p.min_stock,
                        p.unit,
                        p.is_active
                    FROM products p
                    JOIN categories c
                        ON c.id = p.category_id
                    WHERE (
                        %s = ''
                        OR p.product_code ILIKE %s
                        OR p.product_name ILIKE %s
                        OR c.category_name ILIKE %s
                    )
                    ORDER BY p.product_code ASC
                """, (
                    search,
                    f"%{search}%",
                    f"%{search}%",
                    f"%{search}%",
                ))

                product_rows = cur.fetchall()

                cur.execute("""
                    SELECT
                        id,
                        category_code,
                        category_name
                    FROM categories
                    WHERE is_active = true
                    ORDER BY category_code ASC
                """)

                category_rows = cur.fetchall()

        return render_template(
            "products.html",
            products=product_rows,
            categories=category_rows,
            search=search,
        )

    except Exception as error:
        return f"<h1>Data produk gagal dimuat ❌</h1><pre>{error}</pre>", 500


@app.route("/products/add", methods=["POST"])
@roles_required("admin")
def add_product():
    category_id = request.form.get("category_id", "").strip()
    product_name = request.form.get("product_name", "").strip()
    purchase_price_raw = request.form.get("purchase_price", "0").strip()
    selling_price_raw = request.form.get("selling_price", "0").strip()
    stock_raw = request.form.get("stock", "0").strip()
    min_stock_raw = request.form.get("min_stock", "0").strip()
    unit = request.form.get("unit", "pcs").strip()
    is_active = request.form.get("is_active") == "on"

    if not category_id or not product_name:
        flash("Kategori dan nama produk wajib diisi.", "error")
        return redirect(url_for("products"))

    allowed_units = {
        "pcs",
        "botol",
        "bungkus",
        "sachet",
        "pack",
        "dus",
        "kotak",
        "karung",
        "kg",
        "gram",
        "liter",
        "ml",
    }

    if unit not in allowed_units:
        unit = "pcs"

    try:
        category_id = int(category_id)
        purchase_price = Decimal(purchase_price_raw or "0")
        selling_price = Decimal(selling_price_raw or "0")
        stock = int(stock_raw or "0")
        min_stock = int(min_stock_raw or "0")

        if purchase_price < 0 or selling_price < 0:
            raise ValueError("Harga tidak boleh minus.")

        if stock < 0 or min_stock < 0:
            raise ValueError("Stok tidak boleh minus.")

        product_code = generate_product_code()

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO products (
                        product_code,
                        category_id,
                        product_name,
                        purchase_price,
                        selling_price,
                        stock,
                        min_stock,
                        unit,
                        is_active
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    product_code,
                    category_id,
                    product_name,
                    purchase_price,
                    selling_price,
                    stock,
                    min_stock,
                    unit,
                    is_active,
                ))

        flash("Produk berhasil ditambahkan.", "success")

    except (ValueError, InvalidOperation) as error:
        flash(f"Data produk tidak valid: {error}", "error")

    except psycopg.errors.ForeignKeyViolation:
        flash("Kategori yang dipilih tidak ditemukan.", "error")

    except Exception as error:
        flash(f"Gagal menambahkan produk: {error}", "error")

    return redirect(url_for("products"))


@app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def edit_product(product_id):
    allowed_units = {
        "pcs",
        "botol",
        "bungkus",
        "sachet",
        "pack",
        "dus",
        "kotak",
        "karung",
        "kg",
        "gram",
        "liter",
        "ml",
    }

    try:
        if request.method == "POST":
            category_id = int(request.form.get("category_id", "0"))
            product_name = request.form.get("product_name", "").strip()
            purchase_price = Decimal(
                request.form.get("purchase_price", "0") or "0"
            )
            selling_price = Decimal(
                request.form.get("selling_price", "0") or "0"
            )
            stock = int(request.form.get("stock", "0") or "0")
            min_stock = int(request.form.get("min_stock", "0") or "0")
            unit = request.form.get("unit", "pcs").strip()
            is_active = request.form.get("is_active") == "on"

            if not category_id or not product_name:
                flash("Kategori dan nama produk wajib diisi.", "error")
                return redirect(
                    url_for("edit_product", product_id=product_id)
                )

            if unit not in allowed_units:
                unit = "pcs"

            if purchase_price < 0 or selling_price < 0:
                raise ValueError("Harga tidak boleh minus.")

            if stock < 0 or min_stock < 0:
                raise ValueError("Stok tidak boleh minus.")

            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE products
                        SET
                            category_id = %s,
                            product_name = %s,
                            purchase_price = %s,
                            selling_price = %s,
                            stock = %s,
                            min_stock = %s,
                            unit = %s,
                            is_active = %s
                        WHERE id = %s
                    """, (
                        category_id,
                        product_name,
                        purchase_price,
                        selling_price,
                        stock,
                        min_stock,
                        unit,
                        is_active,
                        product_id,
                    ))

            flash("Produk berhasil diubah.", "success")
            return redirect(url_for("products"))

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id,
                        product_code,
                        category_id,
                        product_name,
                        purchase_price,
                        selling_price,
                        stock,
                        min_stock,
                        unit,
                        is_active
                    FROM products
                    WHERE id = %s
                """, (product_id,))

                product = cur.fetchone()

                cur.execute("""
                    SELECT
                        id,
                        category_code,
                        category_name
                    FROM categories
                    WHERE is_active = true
                    ORDER BY category_code ASC
                """)

                category_rows = cur.fetchall()

        if not product:
            flash("Produk tidak ditemukan.", "error")
            return redirect(url_for("products"))

        return render_template(
            "edit_product.html",
            product=product,
            categories=category_rows,
        )

    except Exception as error:
        flash(f"Gagal memproses produk: {error}", "error")
        return redirect(url_for("products"))


@app.route("/products/<int:product_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_product(product_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM products
                    WHERE id = %s
                """, (product_id,))

        flash("Produk berhasil dihapus.", "success")

    except psycopg.errors.ForeignKeyViolation:
        flash(
            "Produk tidak bisa dihapus karena sudah dipakai transaksi.",
            "error"
        )

    except Exception as error:
        flash(f"Gagal menghapus produk: {error}", "error")

    return redirect(url_for("products"))


@app.route("/sales/<int:sale_id>/receipt")
@login_required
def sale_receipt(sale_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                if session.get("role") == "customer":
                    cur.execute("""
                        SELECT
                            s.id,
                            s.invoice_number,
                            s.transaction_date,
                            s.total_items,
                            s.subtotal,
                            s.cash_amount,
                            s.change_amount,
                            s.notes,
                            s.status,
                            customer.full_name,
                            COALESCE(cashier.full_name, '-'),
                            s.payment_method,
                            s.payment_reference,
                            s.paid_at,
                            s.handover_at
                        FROM sales s
                        JOIN app_users customer
                            ON customer.id = s.customer_id
                        LEFT JOIN app_users cashier
                            ON cashier.id = s.cashier_id
                        WHERE s.id = %s
                        AND s.customer_id = %s
                    """, (sale_id, session["user_id"]))
                else:
                    cur.execute("""
                        SELECT
                            s.id,
                            s.invoice_number,
                            s.transaction_date,
                            s.total_items,
                            s.subtotal,
                            s.cash_amount,
                            s.change_amount,
                            s.notes,
                            s.status,
                            COALESCE(customer.full_name, 'Customer'),
                            COALESCE(cashier.full_name, '-'),
                            s.payment_method,
                            s.payment_reference,
                            s.paid_at,
                            s.handover_at
                        FROM sales s
                        LEFT JOIN app_users customer
                            ON customer.id = s.customer_id
                        LEFT JOIN app_users cashier
                            ON cashier.id = s.cashier_id
                        WHERE s.id = %s
                    """, (sale_id,))

                sale = cur.fetchone()

                if not sale:
                    flash("Struk pesanan tidak ditemukan.", "error")
                    return redirect(url_for("redirect_by_role"))

                cur.execute("""
                    SELECT
                        product_code,
                        product_name,
                        selling_price,
                        quantity,
                        subtotal
                    FROM sale_items
                    WHERE sale_id = %s
                    ORDER BY id ASC
                """, (sale_id,))

                sale_items = cur.fetchall()

        return render_template(
            "sale_receipt.html",
            sale=sale,
            sale_items=sale_items,
        )

    except Exception as error:
        return f"<h1>Struk gagal dimuat ❌</h1><pre>{error}</pre>", 500


@app.route("/reports")
@roles_required("admin", "kasir")
def reports():
    report_type = request.args.get("report_type", "sales").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    status_filter = request.args.get("status", "").strip()
    category_id_raw = request.args.get("category_id", "").strip()
    keyword = request.args.get("keyword", "").strip()
    stock_filter = request.args.get("stock_filter", "").strip()

    today = datetime.now().date()

    if not date_from:
        date_from = today.replace(day=1).isoformat()

    if not date_to:
        date_to = today.isoformat()

    try:
        category_id = int(category_id_raw) if category_id_raw else 0
    except ValueError:
        category_id = 0

    if report_type not in ("sales", "product_sales", "stock"):
        report_type = "sales"

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id,
                        category_code,
                        category_name
                    FROM categories
                    WHERE is_active = true
                    ORDER BY category_code ASC
                """)

                categories = cur.fetchall()

                if report_type == "sales":
                    cur.execute("""
                        SELECT
                            s.id,
                            s.invoice_number,
                            s.transaction_date,
                            COALESCE(customer.full_name, 'Customer'),
                            COALESCE(cashier.full_name, '-'),
                            s.total_items,
                            s.subtotal,
                            s.cash_amount,
                            s.change_amount,
                            s.status,
                            s.payment_method
                        FROM sales s
                        LEFT JOIN app_users customer
                            ON customer.id = s.customer_id
                        LEFT JOIN app_users cashier
                            ON cashier.id = s.cashier_id
                        WHERE DATE(s.transaction_date) BETWEEN %s AND %s
                        AND (
                            %s = ''
                            OR s.status = %s
                        )
                        AND (
                            %s = ''
                            OR s.invoice_number ILIKE %s
                            OR COALESCE(customer.full_name, '') ILIKE %s
                            OR COALESCE(cashier.full_name, '') ILIKE %s
                        )
                        ORDER BY s.transaction_date ASC
                    """, (
                        date_from,
                        date_to,
                        status_filter,
                        status_filter,
                        keyword,
                        f"%{keyword}%",
                        f"%{keyword}%",
                        f"%{keyword}%",
                    ))

                    report_rows = cur.fetchall()

                    cur.execute("""
                        SELECT
                            COUNT(*),
                            COALESCE(SUM(total_items), 0),
                            COALESCE(SUM(subtotal), 0)
                        FROM sales
                        WHERE DATE(transaction_date) BETWEEN %s AND %s
                        AND status IN ('confirmed', 'completed')
                    """, (date_from, date_to))

                    summary = cur.fetchone()

                elif report_type == "product_sales":
                    cur.execute("""
                        SELECT
                            si.product_code,
                            si.product_name,
                            COALESCE(c.category_name, '-'),
                            SUM(si.quantity) AS total_quantity,
                            SUM(si.subtotal) AS total_sales
                        FROM sale_items si
                        JOIN sales s
                            ON s.id = si.sale_id
                        LEFT JOIN products p
                            ON p.id = si.product_id
                        LEFT JOIN categories c
                            ON c.id = p.category_id
                        WHERE DATE(s.transaction_date) BETWEEN %s AND %s
                        AND s.status IN ('confirmed', 'completed')
                        AND (
                            %s = 0
                            OR p.category_id = %s
                        )
                        AND (
                            %s = ''
                            OR si.product_code ILIKE %s
                            OR si.product_name ILIKE %s
                            OR COALESCE(c.category_name, '') ILIKE %s
                        )
                        GROUP BY
                            si.product_code,
                            si.product_name,
                            c.category_name
                        ORDER BY
                            total_quantity DESC,
                            total_sales DESC
                    """, (
                        date_from,
                        date_to,
                        category_id,
                        category_id,
                        keyword,
                        f"%{keyword}%",
                        f"%{keyword}%",
                        f"%{keyword}%",
                    ))

                    report_rows = cur.fetchall()

                    cur.execute("""
                        SELECT
                            COALESCE(SUM(si.quantity), 0),
                            COALESCE(SUM(si.subtotal), 0),
                            COUNT(DISTINCT si.product_id)
                        FROM sale_items si
                        JOIN sales s
                            ON s.id = si.sale_id
                        LEFT JOIN products p
                            ON p.id = si.product_id
                        WHERE DATE(s.transaction_date) BETWEEN %s AND %s
                        AND s.status IN ('confirmed', 'completed')
                        AND (
                            %s = 0
                            OR p.category_id = %s
                        )
                    """, (
                        date_from,
                        date_to,
                        category_id,
                        category_id,
                    ))

                    summary = cur.fetchone()

                else:
                    cur.execute("""
                        SELECT
                            p.product_code,
                            p.product_name,
                            c.category_name,
                            p.stock,
                            p.min_stock,
                            p.unit,
                            p.selling_price,
                            p.is_active
                        FROM products p
                        JOIN categories c
                            ON c.id = p.category_id
                        WHERE (
                            %s = 0
                            OR p.category_id = %s
                        )
                        AND (
                            %s = ''
                            OR p.product_code ILIKE %s
                            OR p.product_name ILIKE %s
                            OR c.category_name ILIKE %s
                        )
                        AND (
                            %s = ''
                            OR (%s = 'low_stock' AND p.stock <= p.min_stock)
                            OR (%s = 'available' AND p.stock > p.min_stock)
                            OR (%s = 'inactive' AND p.is_active = false)
                        )
                        ORDER BY p.product_code ASC
                    """, (
                        category_id,
                        category_id,
                        keyword,
                        f"%{keyword}%",
                        f"%{keyword}%",
                        f"%{keyword}%",
                        stock_filter,
                        stock_filter,
                        stock_filter,
                        stock_filter,
                    ))

                    report_rows = cur.fetchall()

                    cur.execute("""
                        SELECT
                            COUNT(*),
                            COALESCE(SUM(stock), 0),
                            COALESCE(SUM(
                                CASE
                                    WHEN stock <= min_stock THEN 1
                                    ELSE 0
                                END
                            ), 0)
                        FROM products
                        WHERE (
                            %s = 0
                            OR category_id = %s
                        )
                    """, (category_id, category_id))

                    summary = cur.fetchone()

        return render_template(
            "reports.html",
            report_type=report_type,
            date_from=date_from,
            date_to=date_to,
            status_filter=status_filter,
            category_id=category_id,
            keyword=keyword,
            stock_filter=stock_filter,
            categories=categories,
            report_rows=report_rows,
            summary=summary,
        )

    except Exception as error:
        return f"<h1>Laporan gagal dimuat ❌</h1><pre>{error}</pre>", 500


@app.route("/sales")
@login_required
def sales():
    flash(
        "Transaksi langsung sudah diganti dengan sistem pesanan customer.",
        "error"
    )
    return redirect(url_for("redirect_by_role"))


@app.route("/test-db")
def test_database():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), current_user;")
                database_name, database_user = cur.fetchone()

        return f"""
        <h1>Sahabat Desa Mart berhasil terhubung ke Supabase ✅</h1>
        <p><b>Database:</b> {database_name}</p>
        <p><b>User:</b> {database_user}</p>
        """

    except Exception as error:
        return f"<h1>Koneksi database gagal ❌</h1><pre>{error}</pre>", 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)