
import os
import json
from datetime import datetime, timezone

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
)

import firebase_admin
from firebase_admin import credentials, firestore


# --------------------------------------------------
# Flask configuration
# --------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "online-bookstore-local-key"
)


# --------------------------------------------------
# Firebase configuration
# Render: Set FIREBASE_CREDENTIALS in Environment.
# Local: Keep firebase-key.json in the project folder.
# --------------------------------------------------

if not firebase_admin._apps:
    firebase_json = os.environ.get("FIREBASE_CREDENTIALS")

    if firebase_json:
        credential_data = json.loads(firebase_json)
        cred = credentials.Certificate(credential_data)
    else:
        key_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "firebase-key.json"
        )

        if not os.path.exists(key_path):
            raise RuntimeError(
                "Firebase credentials are missing. Set "
                "FIREBASE_CREDENTIALS on Render or provide "
                "firebase-key.json for local development."
            )

        cred = credentials.Certificate(key_path)

    firebase_admin.initialize_app(cred)

db = firestore.client()
books_ref = db.collection("books")
orders_ref = db.collection("orders")


# --------------------------------------------------
# Helper functions
# --------------------------------------------------

def current_time():
    return datetime.now(timezone.utc).isoformat()


def get_books():
    books = []

    for document in books_ref.stream():
        book = document.to_dict() or {}
        book["id"] = document.id

        # Ensure numeric fields have safe defaults.
        book["price"] = float(book.get("price", 0) or 0)
        book["stock"] = int(book.get("stock", 0) or 0)

        books.append(book)

    return sorted(
        books,
        key=lambda book: str(book.get("title", "")).lower()
    )


def get_orders():
    orders = []

    for document in orders_ref.stream():
        order = document.to_dict() or {}
        order["id"] = document.id
        orders.append(order)

    return sorted(
        orders,
        key=lambda order: str(order.get("created_at", "")),
        reverse=True
    )


def parse_book_form():
    title = request.form.get("title", "").strip()
    author = request.form.get("author", "").strip()
    category = request.form.get("category", "").strip()

    if not title or not author:
        raise ValueError("Book title and author are required.")

    try:
        price = float(request.form.get("price", "0"))
        stock = int(request.form.get("stock", "0"))
    except (TypeError, ValueError):
        raise ValueError("Enter a valid price and stock quantity.")

    if price < 0 or stock < 0:
        raise ValueError("Price and stock cannot be negative.")

    return {
        "title": title,
        "author": author,
        "category": category,
        "price": price,
        "stock": stock,
    }


def find_book(book_id):
    document = books_ref.document(book_id).get()

    if not document.exists:
        return None

    book = document.to_dict() or {}
    book["id"] = document.id
    book["price"] = float(book.get("price", 0) or 0)
    book["stock"] = int(book.get("stock", 0) or 0)

    return book


# --------------------------------------------------
# Home page / dashboard
# --------------------------------------------------

@app.route("/")
def index():
    books = get_books()

    total_books = len(books)
    total_stock = sum(book["stock"] for book in books)

    inventory_value = sum(
        book["price"] * book["stock"]
        for book in books
    )

    return render_template(
        "index.html",
        books=books,
        total_books=total_books,
        total_stock=total_stock,
        total_value=inventory_value,
        inventory_value=inventory_value,
    )


# --------------------------------------------------
# Add a book
# --------------------------------------------------

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

    return render_template("add.html")


# --------------------------------------------------
# Edit a book
# --------------------------------------------------

@app.route("/edit/<book_id>", methods=["GET", "POST"])
def edit_book(book_id):
    book = find_book(book_id)

    if book is None:
        flash("Book not found.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        try:
            book_data = parse_book_form()
            books_ref.document(book_id).update(book_data)

            flash("Book updated successfully!", "success")
            return redirect(url_for("index"))

        except ValueError as error:
            flash(str(error), "error")

    return render_template("edit.html", book=book)


# --------------------------------------------------
# Delete a book
# --------------------------------------------------

@app.route("/delete/<book_id>", methods=["POST"])
def delete_book(book_id):
    book_ref = books_ref.document(book_id)

    if not book_ref.get().exists:
        flash("Book not found.", "error")
    else:
        book_ref.delete()
        flash("Book deleted successfully!", "success")

    return redirect(url_for("index"))


# --------------------------------------------------
# Customer orders
# --------------------------------------------------

@app.route("/orders", methods=["GET", "POST"])
def orders():
    if request.method == "POST":
        customer_name = request.form.get(
            "customer_name", ""
        ).strip()

        book_id = request.form.get("book_id", "").strip()

        try:
            quantity = int(request.form.get("quantity", "1"))
        except (TypeError, ValueError):
            quantity = 0

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
        def create_order(transaction):
            book_snapshot = book_ref.get(transaction=transaction)

            if not book_snapshot.exists:
                raise ValueError("The selected book does not exist.")

            book = book_snapshot.to_dict() or {}
            stock = int(book.get("stock", 0) or 0)
            price = float(book.get("price", 0) or 0)

            if stock < quantity:
                raise ValueError(
                    f"Insufficient stock. Available quantity: {stock}."
                )

            transaction.update(
                book_ref,
                {"stock": stock - quantity}
            )

            transaction.set(
                order_ref,
                {
                    "customer_name": customer_name,
                    "book_id": book_id,
                    "book_title": book.get("title", "Unknown book"),
                    "quantity": quantity,
                    "unit_price": price,
                    "total": price * quantity,
                    "status": "Pending",
                    "created_at": current_time(),
                }
            )

        try:
            create_order(transaction)
            flash("Customer order created successfully!", "success")

        except ValueError as error:
            flash(str(error), "error")

        return redirect(url_for("orders"))

    return render_template(
        "orders.html",
        orders=get_orders(),
        books=get_books()
    )


# --------------------------------------------------
# Update order status
# --------------------------------------------------

@app.route("/order-status/<order_id>", methods=["POST"])
def update_order_status(order_id):
    new_status = request.form.get("status", "").strip()

    allowed_statuses = {
        "Pending",
        "Processing",
        "Completed",
        "Cancelled",
    }

    if new_status not in allowed_statuses:
        flash("Invalid order status.", "error")
        return redirect(url_for("orders"))

    order_ref = orders_ref.document(order_id)
    transaction = db.transaction()

    @firestore.transactional
    def change_status(transaction):
        order_snapshot = order_ref.get(transaction=transaction)

        if not order_snapshot.exists:
            raise ValueError("Order not found.")

        order = order_snapshot.to_dict() or {}
        old_status = order.get("status", "Pending")

        if old_status == "Cancelled" and new_status != "Cancelled":
            raise ValueError(
                "A cancelled order cannot be reopened."
            )

        if old_status == new_status:
            return

        if new_status == "Cancelled":
            book_ref = books_ref.document(order["book_id"])
            book_snapshot = book_ref.get(transaction=transaction)

            if book_snapshot.exists:
                book = book_snapshot.to_dict() or {}
                stock = int(book.get("stock", 0) or 0)
                quantity = int(order.get("quantity", 0) or 0)

                transaction.update(
                    book_ref,
                    {"stock": stock + quantity}
                )

        transaction.update(
            order_ref,
            {"status": new_status}
        )

    try:
        change_status(transaction)
        flash("Order status updated successfully!", "success")

    except ValueError as error:
        flash(str(error), "error")

    return redirect(url_for("orders"))


# --------------------------------------------------
# Delete an order
# --------------------------------------------------

@app.route("/order-delete/<order_id>", methods=["POST"])
def delete_order(order_id):
    order_ref = orders_ref.document(order_id)
    transaction = db.transaction()

    @firestore.transactional
    def remove_order(transaction):
        order_snapshot = order_ref.get(transaction=transaction)

        if not order_snapshot.exists:
            raise ValueError("Order not found.")

        order = order_snapshot.to_dict() or {}

        # Restore stock if the order was not already cancelled.
        if order.get("status") != "Cancelled":
            book_id = order.get("book_id")

            if book_id:
                book_ref = books_ref.document(book_id)
                book_snapshot = book_ref.get(transaction=transaction)

                if book_snapshot.exists:
                    book = book_snapshot.to_dict() or {}
                    stock = int(book.get("stock", 0) or 0)
                    quantity = int(order.get("quantity", 0) or 0)

                    transaction.update(
                        book_ref,
                        {"stock": stock + quantity}
                    )

        transaction.delete(order_ref)

    try:
        remove_order(transaction)
        flash("Order deleted successfully!", "success")

    except ValueError as error:
        flash(str(error), "error")

    return redirect(url_for("orders"))


# --------------------------------------------------
# Run locally
# --------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
