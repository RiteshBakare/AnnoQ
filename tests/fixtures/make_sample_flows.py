"""Generate synthetic wireframe flows for prompt tuning (T2.4/T2.5): shopping (4), login_otp (3), onboarding (4).

Not real designs; replace with the golden set in T6.1. Run:  python -m tests.fixtures.make_sample_flows [out_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 390, 844
INK, MUTED, LINE, BRAND, OK, BG = "#1f2328", "#6b778c", "#d0d7de", "#0b5cff", "#1a7f37", "#ffffff"


def font(size: int, bold: bool = False):
    for name in (("arialbd.ttf", "DejaVuSans-Bold.ttf") if bold else ("arial.ttf", "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


class Screen:
    def __init__(self, title: str, back: bool = True):
        self.img = Image.new("RGB", (W, H), BG)
        self.d = ImageDraw.Draw(self.img)
        self.y = 20
        self.d.text((16, 14), "9:41", font=font(13), fill=INK)
        self.y = 44
        if back:
            self.d.text((16, self.y + 2), "<", font=font(22, True), fill=INK)
        self.d.text((48 if back else 16, self.y + 2), title, font=font(20, True), fill=INK)
        self.y += 44
        self.d.line((0, self.y, W, self.y), fill=LINE)
        self.y += 14

    def text(self, s, size=14, bold=False, color=INK, x=16, gap=8):
        self.d.text((x, self.y), s, font=font(size, bold), fill=color)
        self.y += size + gap

    def row(self, left, right, size=14, bold=False, color=INK):
        self.d.text((16, self.y), left, font=font(size, bold), fill=color)
        f = font(size, bold)
        self.d.text((W - 16 - self.d.textlength(right, font=f), self.y), right, font=f, fill=color)
        self.y += size + 10

    def stepper(self, steps, active):
        x = 16
        for i, s in enumerate(steps):
            c = BRAND if i <= active else MUTED
            self.d.ellipse((x, self.y, x + 18, self.y + 18), fill=c)
            self.d.text((x + 5, self.y + 1), str(i + 1), font=font(12, True), fill="white")
            self.d.text((x + 24, self.y + 1), s, font=font(13, i == active), fill=c)
            x += 120
        self.y += 34

    def input(self, label, value="", hint=""):
        self.text(label, 12, color=MUTED, gap=4)
        self.d.rounded_rectangle((16, self.y, W - 16, self.y + 40), 6, outline=LINE)
        self.d.text((26, self.y + 11), value or hint, font=font(14), fill=INK if value else MUTED)
        self.y += 52

    def button(self, label, primary=True, disabled=False):
        fill = "#c7cdd4" if disabled else (BRAND if primary else BG)
        self.d.rounded_rectangle((16, self.y, W - 16, self.y + 48), 8, fill=fill, outline=BRAND if not primary else None)
        f = font(16, True)
        self.d.text(((W - self.d.textlength(label, font=f)) / 2, self.y + 14), label, font=f,
                    fill="white" if primary else BRAND)
        self.y += 60

    def link(self, label, center=True):
        f = font(14)
        x = (W - self.d.textlength(label, font=f)) / 2 if center else 16
        self.d.text((x, self.y), label, font=f, fill=BRAND)
        self.y += 26

    def card(self, lines, action=None, height=None):
        h = height or 18 + 22 * len(lines)
        self.d.rounded_rectangle((16, self.y, W - 16, self.y + h), 8, outline=LINE)
        yy = self.y + 10
        for i, ln in enumerate(lines):
            self.d.text((28, yy), ln, font=font(14, i == 0), fill=INK if i == 0 else MUTED)
            yy += 22
        if action:
            f = font(14, True)
            self.d.text((W - 28 - self.d.textlength(action, font=f), self.y + 10), action, font=f, fill=BRAND)
        self.y += h + 14

    def radio(self, label, sub="", selected=False):
        self.d.ellipse((16, self.y + 2, 34, self.y + 20), outline=BRAND if selected else MUTED, width=2)
        if selected:
            self.d.ellipse((21, self.y + 7, 29, self.y + 15), fill=BRAND)
        self.d.text((44, self.y + 2), label, font=font(14, selected), fill=INK)
        if sub:
            f = font(13)
            self.d.text((W - 16 - self.d.textlength(sub, font=f), self.y + 3), sub, font=f, fill=MUTED)
        self.y += 32

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.img.save(path)


def address(s: Screen, action="Change"):
    s.card(["Home - Ritu Sharma", "12, MG Road, Koregaon Park", "Pune, MH 411001", "+91 98765 43210"], action)


def order_summary(s: Screen, compact=False):
    s.text("Order summary", 16, True)
    if not compact:
        s.row("Subtotal (3 items)", "Rs. 2,497")
        s.row("Shipping", "FREE", color=OK)
        s.row("Discount (SAVE10)", "- Rs. 250", color=OK)
    s.row("Total", "Rs. 2,247", 16, True)
    s.y += 6


def shopping(out: Path):
    s = Screen("My Cart (3)")
    for name, meta, price in [("Cotton Crew T-Shirt", "Size M  |  Blue", "Rs. 799"),
                              ("Slim Fit Jeans", "Size 32  |  Black", "Rs. 1,299"),
                              ("Canvas Sneakers", "Size 8  |  White", "Rs. 399")]:
        s.d.rounded_rectangle((16, s.y, 80, s.y + 64), 6, fill="#eef1f4")
        s.d.text((92, s.y), name, font=font(14, True), fill=INK)
        s.d.text((92, s.y + 20), meta, font=font(12), fill=MUTED)
        s.d.text((92, s.y + 42), price, font=font(14, True), fill=INK)
        s.d.rounded_rectangle((260, s.y + 36, 340, s.y + 62), 6, outline=LINE)
        s.d.text((270, s.y + 40), "-    1    +", font=font(14), fill=INK)
        s.d.text((300, s.y), "Remove", font=font(12), fill="#cf222e")
        s.y += 80
    s.input("Promo code", "SAVE10")
    s.d.text((W - 70, s.y - 40), "Apply", font=font(14, True), fill=BRAND)
    s.text("SAVE10 applied: 10% off", 12, color=OK)
    order_summary(s)
    s.button("Proceed to checkout")
    s.save(out / "01_cart.png")

    s = Screen("Checkout")
    s.stepper(["Shipping", "Payment", "Review"], 0)
    s.text("Deliver to", 16, True)
    address(s)
    s.link("+ Add new address", center=False)
    s.input("Full name", hint="Enter full name")
    s.input("Phone number", hint="10-digit mobile number")
    s.input("Pincode", hint="6 digits")
    s.input("Address line", hint="House no, street, area")
    s.text("Delivery speed", 16, True)
    s.radio("Standard (3-5 days)", "FREE", True)
    s.radio("Express (1 day)", "Rs. 99")
    s.button("Continue to payment")
    s.save(out / "02_shipping.png")

    s = Screen("Checkout")
    s.stepper(["Shipping", "Payment", "Review"], 2)
    s.text("Deliver to", 16, True)
    address(s)
    s.text("Payment method", 16, True)
    s.radio("UPI", "Google Pay, PhonePe", True)
    s.radio("Credit / Debit card")
    s.radio("Cash on delivery", "+ Rs. 40")
    s.y += 6
    order_summary(s)
    s.text("By placing the order you agree to our Terms & Returns policy.", 11, color=MUTED)
    s.button("Place order  -  Rs. 2,247")
    s.save(out / "03_place_order.png")

    s = Screen("Order confirmed", back=False)
    s.d.ellipse((W / 2 - 36, s.y + 10, W / 2 + 36, s.y + 82), fill=OK)
    s.d.text((W / 2 - 12, s.y + 26), "OK", font=font(18, True), fill="white")
    s.y += 100
    s.text("Order placed!", 22, True, x=120)
    s.text("Order #ORD-10293  |  Estimated delivery Fri, 21 Nov", 13, color=MUTED, x=30)
    s.y += 6
    s.text("Delivering to", 16, True)
    address(s, action=None)
    order_summary(s, compact=True)
    s.button("Track order")
    s.button("Continue shopping", primary=False)
    s.save(out / "04_order_success.png")


def login_otp(out: Path):
    s = Screen("", back=False)
    s.y += 40
    s.d.rounded_rectangle((W / 2 - 32, s.y, W / 2 + 32, s.y + 64), 14, fill=BRAND)
    s.y += 90
    s.text("Welcome back", 24, True, x=110)
    s.text("Log in with your mobile number", 14, color=MUTED, x=92)
    s.y += 20
    s.input("Mobile number", "+91  98765 43210")
    s.button("Send OTP")
    s.link("Log in with email instead")
    s.y += 300
    s.text("By continuing you agree to the Terms of Service and Privacy Policy", 11, color=MUTED, x=24)
    s.save(out / "01_login.png")

    s = Screen("Verify number")
    s.text("Enter the 6-digit code sent to", 14, color=MUTED)
    s.text("+91 98765 43210", 16, True)
    s.link("Change number", center=False)
    x = 16
    for i in range(6):
        s.d.rounded_rectangle((x, s.y, x + 50, s.y + 56), 8, outline=BRAND if i == 4 else LINE, width=2)
        if i < 4:
            s.d.text((x + 18, s.y + 15), "4721"[i], font=font(20, True), fill=INK)
        x += 60
    s.y += 72
    s.text("Incorrect code. 2 attempts left.", 12, color="#cf222e")
    s.text("Resend code in 00:28", 13, color=MUTED)
    s.button("Verify", disabled=True)
    s.save(out / "02_otp.png")

    s = Screen("Complete your profile")
    s.d.ellipse((W / 2 - 40, s.y, W / 2 + 40, s.y + 80), fill="#eef1f4")
    s.d.text((W / 2 - 22, s.y + 30), "Photo", font=font(13), fill=MUTED)
    s.y += 96
    s.input("Full name *", hint="As on your ID")
    s.input("Email", hint="name@example.com")
    s.input("Date of birth", hint="DD/MM/YYYY")
    s.text("Gender", 12, color=MUTED, gap=4)
    s.radio("Female")
    s.radio("Male")
    s.radio("Prefer not to say")
    s.button("Save and continue")
    s.link("Skip for now")
    s.save(out / "03_profile_setup.png")


def onboarding(out: Path):
    def carousel(n, title, body, name):
        s = Screen("", back=False)
        s.d.text((W - 56, 52), "Skip", font=font(15, True), fill=BRAND)
        s.d.rounded_rectangle((40, s.y + 30, W - 40, s.y + 330), 20, fill="#eef1f4")
        s.d.text((W / 2 - 50, s.y + 170), "Illustration", font=font(16), fill=MUTED)
        s.y += 370
        s.text(title, 24, True, x=40)
        for ln in body:
            s.text(ln, 15, color=MUTED, x=40, gap=6)
        s.y = 700
        for i in range(3):
            cx = W / 2 - 30 + i * 30
            s.d.ellipse((cx - 5, s.y, cx + 5, s.y + 10), fill=BRAND if i == n else LINE)
        s.y += 30
        s.button("Next" if n < 2 else "Get started")
        s.save(out / name)

    carousel(0, "Track every rupee", ["See where your money goes with", "automatic categories."], "01_welcome.png")
    carousel(1, "Budgets that adapt", ["Set monthly limits and get alerts", "before you overspend."], "02_budgets.png")
    carousel(2, "Bank-grade security", ["Your data is encrypted and never", "sold to anyone."], "03_security.png")

    s = Screen("Pick your interests")
    s.text("Choose at least 3 to personalise your feed", 14, color=MUTED)
    x, y0 = 16, s.y + 6
    chips = ["Investing", "Savings", "Credit cards", "Loans", "Crypto", "Tax saving", "Insurance", "Budgeting"]
    for i, c in enumerate(chips):
        f = font(14, i in (0, 5))
        w = s.d.textlength(c, font=f) + 28
        if x + w > W - 16:
            x, y0 = 16, y0 + 48
        s.d.rounded_rectangle((x, y0, x + w, y0 + 36), 18, fill=BRAND if i in (0, 5) else BG,
                              outline=BRAND if i in (0, 5) else LINE)
        s.d.text((x + 14, y0 + 9), c, font=f, fill="white" if i in (0, 5) else INK)
        x += w + 10
    s.y = y0 + 60
    s.text("2 of 3 selected", 13, color=MUTED)
    s.y = 700
    s.button("Continue", disabled=True)
    s.save(out / "04_interests.png")


def main(out_dir: str = "flows") -> None:
    root = Path(out_dir)
    shopping(root / "shopping")
    login_otp(root / "login_otp")
    onboarding(root / "onboarding")
    print("wrote", sorted(str(p) for p in root.glob("*/*.png")))


if __name__ == "__main__":
    main(*sys.argv[1:])
