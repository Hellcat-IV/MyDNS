import ipaddress
import re

from flask_wtf import FlaskForm
from wtforms import BooleanField, IntegerField, PasswordField, SelectField, StringField
from wtforms.validators import DataRequired, Length, NumberRange, ValidationError

RECORD_TYPES = ["A", "AAAA", "CNAME", "MX", "TXT", "PTR"]

_HOSTNAME_RE = re.compile(
    r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.?$"
)


def _normalize_fqdn(name: str) -> str:
    name = name.strip().lower()
    if not name.endswith("."):
        name += "."
    return name


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(1, 64)])
    password = PasswordField("Password", validators=[DataRequired()])


class RecordForm(FlaskForm):
    name = StringField("Name (FQDN)", validators=[DataRequired(), Length(1, 253)])
    type = SelectField("Type", choices=[(t, t) for t in RECORD_TYPES])
    value = StringField("Value", validators=[DataRequired(), Length(1, 512)])
    ttl = IntegerField("TTL (seconds)", validators=[NumberRange(60, 86400)], default=300)
    priority = IntegerField("Priority (MX only)", validators=[NumberRange(0, 65535)], default=0)
    enabled = BooleanField("Enabled", default=True)

    def validate_name(self, field):
        raw = field.data.strip().rstrip(".")
        if len(raw) < 1 or len(raw) > 253:
            raise ValidationError("Name must be 1–253 characters (excluding trailing dot).")
        if not _HOSTNAME_RE.match(raw + "."):
            raise ValidationError("Invalid FQDN format.")
        field.data = _normalize_fqdn(field.data)

    def validate_value(self, field):
        rtype = self.type.data
        value = field.data.strip()
        try:
            if rtype == "A":
                addr = ipaddress.ip_address(value)
                if not isinstance(addr, ipaddress.IPv4Address):
                    raise ValidationError("A record requires an IPv4 address.")
            elif rtype == "AAAA":
                addr = ipaddress.ip_address(value)
                if not isinstance(addr, ipaddress.IPv6Address):
                    raise ValidationError("AAAA record requires an IPv6 address.")
            elif rtype in ("CNAME", "MX", "PTR"):
                hostname = value.rstrip(".")
                if not _HOSTNAME_RE.match(hostname + "."):
                    raise ValidationError(f"{rtype} record requires a valid hostname.")
            elif rtype == "TXT":
                if len(value) > 255:
                    raise ValidationError("TXT value must be ≤ 255 characters.")
        except ValidationError:
            raise
        except Exception:
            raise ValidationError(f"Invalid value for {rtype} record.")


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current Password", validators=[DataRequired()])
    new_password = PasswordField(
        "New Password", validators=[DataRequired(), Length(10, 128)]
    )
    confirm_password = PasswordField(
        "Confirm New Password", validators=[DataRequired()]
    )

    def validate_confirm_password(self, field):
        if field.data != self.new_password.data:
            raise ValidationError("Passwords do not match.")
