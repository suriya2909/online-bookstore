
from flask import Flask, render_template, request, redirect, url_for, flash
import firebase_admin
from firebase_admin import credentials, firestore
import os
import json

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "bookstore-lab-secret")

# Firebase connection
if not firebase_admin._apps:
    firebase_json = os.environ.get("FIREBASE_CREDENTIALS")

    if firebase_json:
        cred = credentials.Certificate(json.loads(firebase_json))
    else:
        cred = credentials.Certificate("firebase-key.json")

    firebase_admin.initialize_app(cred)

db = firestore.client()
books_ref = db.collection("books")
orders_ref = db.collection("orders")


def get_books():
    result = []
    for doc in books_ref.stream():
        book = doc.to_dict()
        book["id"] = doc.id
        result.append(book)
    return result


def get_orders():
    result = []
    for doc in orders_ref.stream():
        order = doc.to_dict()
        order["id"] = doc.id
        result.append(order)
    return result


@app.route("/")
def index():
    books = get_books()
    total_books = len(books)
    inventory_value = sum(
        float(b.get("price", 0)) * int(b.get("quantity", 0))
        for b in books
    )
    low_stock = sum(
        1 for b in books if int(b.get("quantity", 0)) <= 5
    )
    orders = get_orders()

    return render_template(
        "index.html",
        books=books,
        total_books=total_books,
        inventory_value=inventory_value,
        low_stock=low_stock,
        total_orders=len(orders)
    )


# CREATE book
@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        author = request.form.get("author", "").strip()
        category = request.form.get("category", "General").strip()

        try:
            price = float(request.form.get("price", ""))
            quantity = int(request.form.get("quantity", ""))
        except ValueError:
            flash("Enter a valid price and quantity.", "error")
            return redirect(url_for("add"))

        if not title or not author or price < 0 or quantity < 0:
            flash("Please enter valid book details.", "error")
            return redirect(url_for("add"))

        books_ref.add({
            "title": title,
            "author": author,
            "category": category,
            "price": price,
            "quantity": quantity
        })

        flash("Book added successfully!", "success")
        return redirect(url_for("index"))

    return render_template("add.html", book=None)


# UPDATE book
@app.route("/edit/<id>", methods=["GET", "POST"])
def edit(id):
    ref = books_ref.document(id)
    snapshot = ref.get()

    if not snapshot.exists:
        flash("Book not found.", "error")
        return redirect(url_for("index"))

    book = snapshot.to_dict()
    book["id"] = snapshot.id

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        author = request.form.get("author", "").strip()
        category = request.form.get("category", "General").strip()

        try:
            price = float(request.form.get("price", ""))
            quantity = int(request.form.get("quantity", ""))
        except ValueError:
            flash("Enter a valid price and quantity.", "error")
            return redirect(url_for("edit", id=id))

        if not title or not author or price < 0 or quantity < 0:
            flash("Please enter valid book details.", "error")
            return redirect(url_for("edit", id=id))

        ref.update({
            "title": title,
            "author": author,
            "category": category,
            "price": price,
            "quantity": quantity
        })

        flash("Book updated successfully!", "success")
        return redirect(url_for("index"))

    return render_template("edit.html", book=book)


# DELETE book
@app.route("/delete/<id>", methods=["POST"])
def delete(id):
    ref = books_ref.document(id)

    if not ref.get().exists:
        flash("Book not found.", "error")
    else:
        ref.delete()
        flash("Book deleted successfully.", "success")

    return redirect(url_for("index"))


# READ orders and CREATE orders
@app.route("/orders", methods=["GET", "POST"])
def orders():
    if request.method == "POST":
        customer = request.form.get("customer", "").strip()
        book_id = request.form.get("book_id", "").strip()

        try:
            quantity = int(request.form.get("quantity", ""))
        except ValueError:
            flash("Enter a valid order quantity.", "error")
            return redirect(url_for("orders"))

        if not customer or not book_id or quantity < 1:
            flash("Enter valid order details.", "error")
            return redirect(url_for("orders"))

        book_ref = books_ref.document(book_id)
        transaction = db.transaction()

        @firestore.transactional
        def create_order(transaction):
            snapshot = book_ref.get(transaction=transaction)

            if not snapshot.exists:
                raise ValueError("Book not found.")

            book = snapshot.to_dict()
            stock = int(book.get("quantity", 0))

            if quantity > stock:
                raise ValueError("Not enough stock available.")

            transaction.update(book_ref, {
                "quantity": stock - quantity
            })

            new_order = orders_ref.document()
            transaction.create(new_order, {
                "customer": customer,
                "book_id": book_id,
                "book_title": book.get("title", ""),
                "quantity": quantity,
                "status": "Pending"
            })

        try:
            create_order(transaction)
            flash("Order placed successfully!", "success")
        except ValueError as error:
            flash(str(error), "error")

        return redirect(url_for("orders"))

    return render_template(
        "orders.html",
        books=get_books(),
        orders=get_orders()
    )


# UPDATE order status
@app.route("/order-status/<id>", methods=["POST"])
def order_status(id):
    status = request.form.get("status", "")
    allowed = ["Pending", "Processing", "Shipped",
               "Delivered", "Cancelled"]

    ref = orders_ref.document(id)

    if status not in allowed:
        flash("Invalid order status.", "error")
    elif not ref.get().exists:
        flash("Order not found.", "error")
    else:
        ref.update({"status": status})
        flash("Order status updated.", "success")

    return redirect(url_for("orders"))


# DELETE order
@app.route("/order-delete/<id>", methods=["POST"])
def order_delete(id):
    ref = orders_ref.document(id)

    if ref.get().exists:
        ref.delete()
        flash("Order deleted.", "success")
    else:
        flash("Order not found.", "error")

    return redirect(url_for("orders"))


if __name__ == "__main__":
    app.run(debug=True)
