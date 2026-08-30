"""Register an existing testcase library as formal revision 0.

Run after ``20260812_testcase_library_promotions.sql`` and before enabling the
promotion endpoint. It is idempotent and never changes testcase content.
"""

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'
if str(WEB) not in sys.path:
    sys.path.insert(0, str(WEB))

from app import create_app, db  # noqa: E402
from app.models import TestCaseLibrary  # noqa: E402
from app.services.testcase_library_versioning import (  # noqa: E402
    ensure_library_revision,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--library-id', type=int, default=33,
        help='existing production library to initialize (default: 33)')
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        library = (TestCaseLibrary.query.filter_by(id=args.library_id)
                   .with_for_update().first())
        if library is None:
            raise SystemExit(f'testcase library #{args.library_id} was not found')
        row, drifted = ensure_library_revision(library, 'migration')
        db.session.commit()
        print(
            f'library={library.id} revision={row.revision} '
            f'case_count={row.case_count} content_hash={row.content_hash} '
            f'drifted={str(drifted).lower()}')


if __name__ == '__main__':
    main()
