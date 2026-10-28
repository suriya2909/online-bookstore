
import os
import json
from datetime import datetime, timezone

from flask import Flask, render_template, request, redirect, url_for, flash
import firebase_admin
from firebase_admin import credentials, firestore


# --------------------------------------------------
# Flask configuration
# --------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "online-bookstore-local-key")


# --------------------------------------------------
# Firebase / Firestore configuration
# Supports Render environment variables and local use
# --------------------------------------------------

if not firebase_admin._apps:
    firebase_json = os.environ.get("FIREBASE_CREDENTIALS")

    if firebase_json:
        # Render: read credentials from environment variable
        credential_data = json.loads(firebase_json)
        cred = credentials.Certificate(credential_data)
    else:
        # Local computer: read the private JSON file
        key_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "firebase-key.json"
        )

        if not os.path.exists(key_path):
            raise RuntimeError(
                "Firebase credentials are missing. Set FIREBASE_CREDENTIALS "
                "or provide firebase-key.json for local development."
            )

        cred = credentials.Certificate(key_path)

    firebase_admin.initialize_app(cred)

db = firestore.client()

books_ref = db.collection("books")
orders_ref = db.collection("orders")


# --------------------------------------------------
# Helper functions
# --------------------------------------------------

def get_books():
    books = []

    for document in books_ref.stream():
        book = document.to_dict()
        book["id"] = document.id
        books.append(book)

    return books


def get_orders():
    orders = []

    for document in orders_ref.stream():
        order = document.to_dict()
        order["id"] = document.id
        orders.append(order)

    return orders


def get_book(book_id):
    document = books_ref.document(book_id).get()

    if not document.exists:
        return None

    book = document.to_dict()
    book["id"] = document.id
    return book


def parse_book_form():
    """Read and validate book details from an HTML form."""
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
        "stock": stock
    }


# --------------------------------------------------
# Dashboard - display all books
# --------------------------------------------------

@app.route("/")
def index():
    books = get_books()

    total_books = len(books)
    total_stock = sum(int(book.get("stock", 0)) for book in books)
    total_value = sum(
        float(book.get("price", 0)) * int(book.get("stock", 0))
        for book in books
    )

    return render_template(
        "index.html",
        books=books,
        total_books=total_books,
        total_stock=total_stock,
        total_value=total_value
    )


# --------------------------------------------------
# Add a new book
# --------------------------------------------------

@app.route("/add", methods=["GET", "POST"])
def add_book():
    if request.method == "POST":
        try:
            book_data = parse_book_form()
            book_data["created_at"] = datetime.now(timezone.utc)
            books_ref.add(book_data)

            flash("Book added successfully!", "success")
            return redirect(url_for("index"))

        except ValueError as error:
            flash(str(error), "error")

    return render_template("add.html")


# --------------------------------------------------
# Edit an existing book
# --------------------------------------------------

@app.route("/edit/<book_id>", methods=["GET", "POST"])
def edit_book(book_id):
    book = get_book(book_id)

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
# Customer orders - display and create orders
# --------------------------------------------------

@app.route("/orders", methods=["GET", "POST"])
def orders():
    if request.method == "POST":
        customer_name = request.form.get("customer_name", "").strip()
        book_id = request.form.get("book_id", "").strip()

        try:
            quantity = int(request.form.get("quantity", "1"))
        except (TypeError, ValueError):
            flash("Enter a valid order quantity.", "error")
            return redirect(url_for("orders"))

        if not customer_name or not book_id:
            flash("Customer name and book are required.", "error")
            return redirect(url_for("orders"))

        if quantity <= 0:
            flash("Quantity must be greater than zero.", "error")
            return redirect(url_for("orders"))

        book_ref = books_ref.document(book_id)
        order_ref = orders_ref.document()
        transaction = db.transaction()

        @firestore.transactional
        def create_order(transaction):
            book_snapshot = book_ref.get(transaction=transaction)

            if not book_snapshot.exists:
                raise ValueError("The selected book does not exist.")

            book = book_snapshot.to_dict()
            available_stock = int(book.get("stock", 0))

            if quantity > available_stock:
                raise ValueError(
                    f"Only {available_stock} copies are available."
                )

            order_data = {
                "customer_name": customer_name,
                "book_id": book_id,
                "book_title": book.get("title", "Unknown book"),
                "quantity": quantity,
                "unit_price": float(book.get("price", 0)),
                "total": float(book.get("price", 0)) * quantity,
                "status": "Pending",
                "created_at": datetime.now(timezone.utc)
            }

            transaction.update(
                book_ref,
                {"stock": available_stock - quantity}
            )

            transaction.set(order_ref, order_data)

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
# Update an order's status
# --------------------------------------------------

@app.route("/order-status/<order_id>", methods=["POST"])
def update_order_status(order_id):
    order_ref = orders_ref.document(order_id)
    order_snapshot = order_ref.get()

    if not order_snapshot.exists:
        flash("Order not found.", "error")
        return redirect(url_for("orders"))

    new_status = request.form.get("status", "").strip()
    allowed_statuses = {
        "Pending",
        "Processing",
        "Completed",
        "Cancelled"
    }

    if new_status not in allowed_statuses:
        flash("Invalid order status.", "error")
        return redirect(url_for("orders"))

    order = order_snapshot.to_dict()
    old_status = order.get("status", "Pending")

    # Restore stock if an order is cancelled.
    if old_status != "Cancelled" and new_status == "Cancelled":
        book_ref = books_ref.document(order.get("book_id", ""))

        @firestore.transactional
        def cancel_order(transaction):
            current_order = transaction.get(order_ref)
            current_book = transaction.get(book_ref)

            if not current_order.exists:
                raise ValueError("Order not found.")

            current_data = current_order.to_dict()

            if current_data.get("status") == "Cancelled":
                return

            if current_book.exists:
                current_stock = int(
                    current_book.to_dict().get("stock", 0)
                )
                transaction.update(
                    book_ref,
                    {
                        "stock": current_stock
                        + int(current_data.get("quantity", 0))
                    }
                )

            transaction.update(
                order_ref,
                {"status": "Cancelled"}
            )

        try:
            cancel_order(db.transaction())
            flash("Order cancelled and stock restored.", "success")
        except ValueError as error:
            flash(str(error), "error")

    elif old_status == "Cancelled" and new_status != "Cancelled":
        flash(
            "A cancelled order cannot be reopened automatically. "
            "Create a new order instead.",
            "error"
        )
    else:
        order_ref.update({"status": new_status})
        flash("Order status updated successfully!", "success")

    return redirect(url_for("orders"))


# --------------------------------------------------
# Delete an order
# --------------------------------------------------

@app.route("/order-delete/<order_id>", methods=["POST"])
def delete_order(order_id):
    order_ref = orders_ref.document(order_id)
    order_snapshot = order_ref.get()

    if not order_snapshot.exists:
        flash("Order not found.", "error")
        return redirect(url_for("orders"))

    order = order_snapshot.to_dict()

    # Return stock when deleting an order that is not cancelled.
    if order.get("status") != "Cancelled":
        book_id = order.get("book_id", "")
        book_ref = books_ref.document(book_id)

        @firestore.transactional
        def remove_order(transaction):
            current_order = transaction.get(order_ref)

            if not current_order.exists:
                return

            current_data = current_order.to_dict()
            current_book = transaction.get(book_ref)

            if (
                current_data.get("status") != "Cancelled"
                and current_book.exists
            ):
                stock = int(current_book.to_dict().get("stock", 0))
                transaction.update(
                    book_ref,
                    {
                        "stock": stock
                        + int(current_data.get("quantity", 0))
                    }
                )

            transaction.delete(order_ref)

        remove_order(db.transaction())
    else:
        order_ref.delete()

    flash("Order deleted successfully.", "success")
    return redirect(url_for("orders"))


# --------------------------------------------------
# Run locally
# Render starts the app using: gunicorn app:app
# --------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True)
