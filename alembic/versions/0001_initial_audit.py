"""initial audit tables"""

from alembic import op
from app.persistence.models import metadata

revision = "0001_initial_audit"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    metadata.create_all(op.get_bind())


def downgrade():
    metadata.drop_all(op.get_bind())
