import os
import json
import logging
from datetime import datetime, timezone

from flask import Flask, render_template, request, redirect, url_for, flash
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "online-bookstore-local-key")
logging.basicConfig(level=logging.INFO)

# Firebase credentials: Render uses FIREBASE_CREDENTIALS; local development
# uses firebase-key.json. Never commit the service-account key.
if not firebase_admin._apps:
    firebase_json = os.environ.get("FIREBASE_CREDENTIALS")
    if firebase_json:
        cred = credentials.Certificate(json.loads(firebase_json))
    else:
        key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firebase-key.json")
        if not os.path.exists(key_path):
            raise RuntimeError(
                "Firebase credentials missing. Set FIREBASE_CREDENTIALS on Render "
                "or place firebase-key.json in the project folder for local use."
            )
        cred = credentials.Certificate(key_path)
    firebase_admin.initialize_app(cred)

db = firestore.client()
books_ref = db.collection("books")
orders_ref = db.collection("orders")

ORDER_STATUSES = {"Pending", "Processing", "Completed", "Cancelled"}


def current_time():
    return datetime.now(timezone.utc).isoformat()


def as_int(value, default=0):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def as_float(value, default=0.0):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def normalize_book(document):
    book = document.to_dict() or {}
    book["id"] = document.id
    # Support older records that stored stock under "quantity".
    book["stock"] = as_int(book.get("stock", book.get("quantity", 0)))
    book["price"] = as_float(book.get("price", 0))
    book["title"] = str(book.get("title", "Untitled"))
    book["author"] = str(book.get("author", "Unknown Author"))
    book["category"] = str(book.get("category", "General"))
    return book


def get_books():
    books = [normalize_book(doc) for doc in books_ref.stream()]
    return sorted(books, key=lambda book: book["title"].lower())


def get_orders():
    orders = []
    for document in orders_ref.stream():
        order = document.to_dict() or {}
        order["id"] = document.id
        # Support older order documents that used "customer".
        order["customer_name"] = order.get("customer_name", order.get("customer", "Unknown"))
        order["quantity"] = as_int(order.get("quantity", 0))
        order["total"] = as_float(order.get("total", 0))
        order["status"] = order.get("status", "Pending")
        orders.append(order)
    return sorted(orders, key=lambda order: str(order.get("created_at", "")), reverse=True)


def parse_book_form():
    title = request.form.get("title", "").strip()
    author = request.form.get("author", "").strip()
    category = request.form.get("category", "General").strip() or "General"
    if not title or not author:
        raise ValueError("Book title and author are required.")
    try:
        price = float(request.form.get("price", ""))
        # Accept both field names to remain compatible with the old templates.
        stock_raw = request.form.get("stock", request.form.get("quantity", ""))
        stock = int(stock_raw)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid price and whole-number stock quantity.")
    if price < 0 or stock < 0:
        raise ValueError("Price and stock cannot be negative.")
    return {"title": title, "author": author, "category": category, "price": price, "stock": stock}


def find_book(book_id):
    document = books_ref.document(book_id).get()
    return normalize_book(document) if document.exists else None


@app.route("/")
def index():
    try:
        books = get_books()
        orders = get_orders()
        inventory_value = sum(book["price"] * book["stock"] for book in books)
        return render_template(
            "index.html",
            books=books,
            total_books=len(books),
            total_stock=sum(book["stock"] for book in books),
            total_value=inventory_value,
            inventory_value=inventory_value,
            low_stock=sum(1 for book in books if book["stock"] <= 5),
            total_orders=len(orders),
        )
    except Exception:
        app.logger.exception("Unable to load inventory from Firestore")
        flash("Could not load inventory from Firebase. Please check Render Logs.", "error")
        return render_template(
            "index.html", books=[], total_books=0, total_stock=0,
            total_value=0, inventory_value=0, low_stock=0, total_orders=0
        )


@app.route("/add", methods=["GET", "POST"])
def add_book():
    if request.method == "POST":
        try:
            book_data = parse_book_form()
            book_data["created_at"] = current_time()
            books_ref.add(book_data)
            flash("Book added successfully!", "success")
            return redirect(url_for("index"))
        except ValueError as error:
            flash(str(error), "error")
        except Exception:
            app.logger.exception("Failed to add book to Firestore")
            flash("Could not save the book to Firebase. Check Render Logs and Firebase credentials.", "error")
    return render_template("add.html")


@app.route("/edit/<book_id>", methods=["GET", "POST"])
def edit_book(book_id):
    try:
        book = find_book(book_id)
        if book is None:
            flash("Book not found.", "error")
            return redirect(url_for("index"))
        if request.method == "POST":
            book_data = parse_book_form()
            book_data["updated_at"] = current_time()
            books_ref.document(book_id).update(book_data)
            flash("Book updated successfully!", "success")
            return redirect(url_for("index"))
        return render_template("edit.html", book=book)
    except ValueError as error:
        flash(str(error), "error")
        book = find_book(book_id)
        return render_template("edit.html", book=book) if book else redirect(url_for("index"))
    except Exception:
        app.logger.exception("Failed to edit book in Firestore")
        flash("Could not update the book in Firebase. Check Render Logs.", "error")
        return redirect(url_for("index"))


@app.route("/delete/<book_id>", methods=["POST"])
def delete_book(book_id):
    try:
        book_ref = books_ref.document(book_id)
        if not book_ref.get().exists:
            flash("Book not found.", "error")
        else:
            book_ref.delete()
            flash("Book deleted successfully!", "success")
    except Exception:
        app.logger.exception("Failed to delete book from Firestore")
        flash("Could not delete the book from Firebase. Check Render Logs.", "error")
    return redirect(url_for("index"))


@app.route("/orders", methods=["GET", "POST"])
def orders():
    if request.method == "POST":
        customer_name = request.form.get("customer_name", request.form.get("customer", "")).strip()
        book_id = request.form.get("book_id", "").strip()
        quantity = as_int(request.form.get("quantity"), 0)

        if not customer_name:
            flash("Enter the customer name.", "error")
            return redirect(url_for("orders"))
        if quantity <= 0:
            flash("Quantity must be greater than zero.", "error")
            return redirect(url_for("orders"))
        if not book_id:
            flash("Please select a book.", "error")
            return redirect(url_for("orders"))

        book_ref = books_ref.document(book_id)
        order_ref = orders_ref.document()
        transaction = db.transaction()

        @firestore.transactional
        def create_order(tx):
            book_snapshot = book_ref.get(transaction=tx)
            if not book_snapshot.exists:
                raise ValueError("The selected book does not exist.")
            book = book_snapshot.to_dict() or {}
            stock = as_int(book.get("stock", book.get("quantity", 0)))
            price = as_float(book.get("price", 0))
            if stock < quantity:
                raise ValueError(f"Insufficient stock. Available quantity: {stock}.")
            tx.update(book_ref, {"stock": stock - quantity})
            tx.set(order_ref, {
                "customer_name": customer_name,
                "book_id": book_id,
                "book_title": book.get("title", "Unknown book"),
                "quantity": quantity,
                "unit_price": price,
                "total": round(price * quantity, 2),
                "status": "Pending",
                "created_at": current_time(),
            })

        try:
            create_order(transaction)
            flash("Customer order created successfully!", "success")
        except ValueError as error:
            flash(str(error), "error")
        except Exception:
            app.logger.exception("Failed to create customer order in Firestore")
            flash("Could not place the order in Firebase. Check Render Logs.", "error")
        return redirect(url_for("orders"))

    try:
        return render_template("orders.html", orders=get_orders(), books=get_books())
    except Exception:
        app.logger.exception("Unable to load customer orders from Firestore")
        flash("Could not load customer orders from Firebase. Check Render Logs.", "error")
        return render_template("orders.html", orders=[], books=[])


@app.route("/order-status/<order_id>", methods=["POST"])
def update_order_status(order_id):
    new_status = request.form.get("status", "").strip()
    if new_status not in ORDER_STATUSES:
        flash("Invalid order status.", "error")
        return redirect(url_for("orders"))

    order_ref = orders_ref.document(order_id)
    transaction = db.transaction()

    @firestore.transactional
    def change_status(tx):
        order_snapshot = order_ref.get(transaction=tx)
        if not order_snapshot.exists:
            raise ValueError("Order not found.")
        order = order_snapshot.to_dict() or {}
        old_status = order.get("status", "Pending")
        if old_status == "Cancelled" and new_status != "Cancelled":
            raise ValueError("A cancelled order cannot be reopened.")
        if old_status == new_status:
            return
        book_ref = None
        book_snapshot = None
        if new_status == "Cancelled" and old_status != "Cancelled":
            book_id = order.get("book_id")
            if book_id:
                book_ref = books_ref.document(book_id)
                book_snapshot = book_ref.get(transaction=tx)
        # All transaction reads happen before writes.
        if book_ref and book_snapshot and book_snapshot.exists:
            book = book_snapshot.to_dict() or {}
            stock = as_int(book.get("stock", book.get("quantity", 0)))
            quantity = as_int(order.get("quantity", 0))
            tx.update(book_ref, {"stock": stock + quantity})
        tx.update(order_ref, {"status": new_status, "updated_at": current_time()})

    try:
        change_status(transaction)
        flash("Order status updated successfully!", "success")
    except ValueError as error:
        flash(str(error), "error")
    except Exception:
        app.logger.exception("Failed to update order status in Firestore")
        flash("Could not update order status in Firebase. Check Render Logs.", "error")
    return redirect(url_for("orders"))


@app.route("/order-delete/<order_id>", methods=["POST"])
def delete_order(order_id):
    order_ref = orders_ref.document(order_id)
    transaction = db.transaction()

    @firestore.transactional
    def remove_order(tx):
        order_snapshot = order_ref.get(transaction=tx)
        if not order_snapshot.exists:
            raise ValueError("Order not found.")
        order = order_snapshot.to_dict() or {}
        book_ref = None
        book_snapshot = None
        if order.get("status") != "Cancelled" and order.get("book_id"):
            book_ref = books_ref.document(order["book_id"])
            book_snapshot = book_ref.get(transaction=tx)
        # All transaction reads happen before writes.
        if book_ref and book_snapshot and book_snapshot.exists:
            book = book_snapshot.to_dict() or {}
            stock = as_int(book.get("stock", book.get("quantity", 0)))
            quantity = as_int(order.get("quantity", 0))
            tx.update(book_ref, {"stock": stock + quantity})
        tx.delete(order_ref)

    try:
        remove_order(transaction)
        flash("Order deleted successfully!", "success")
    except ValueError as error:
        flash(str(error), "error")
    except Exception:
        app.logger.exception("Failed to delete order from Firestore")
        flash("Could not delete the order from Firebase. Check Render Logs.", "error")
    return redirect(url_for("orders"))


if __name__ == "__main__":
    app.run(debug=True)
