import logging
import os
import sys
from itertools import groupby, zip_longest

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from sqlalchemy.exc import OperationalError

from corehq.apps.domain.models import Domain
from couchforms.dbaccessors import get_form_ids_by_type
from couchforms.models import XFormInstance, doc_types
from dimagi.utils.chunked import chunked

from corehq.apps.couch_sql_migration.couchsqlmigration import (
    CASE_DOC_TYPES,
    do_couch_to_sql_migration,
    setup_logging,
)
from corehq.apps.couch_sql_migration.progress import (
    couch_sql_migration_in_progress,
    get_couch_sql_migration_status,
    set_couch_sql_migration_complete,
    set_couch_sql_migration_not_started,
    set_couch_sql_migration_started,
)
from corehq.apps.couch_sql_migration.rewind import rewind_iteration_state
from corehq.apps.couch_sql_migration.statedb import (
    Counts,
    delete_state_db,
    init_state_db,
    open_state_db,
)
from corehq.apps.domain.dbaccessors import get_doc_ids_in_domain_by_type
from corehq.apps.hqcase.dbaccessors import get_case_ids_in_domain
from corehq.form_processor.backends.sql.dbaccessors import (
    CaseAccessorSQL,
    FormAccessorSQL,
)
from corehq.form_processor.models import CommCareCaseSQL, XFormInstanceSQL
from corehq.form_processor.utils import should_use_sql_backend
from corehq.sql_db.util import (
    estimate_partitioned_row_count,
    paginate_query_across_partitioned_databases,
)
from corehq.util.log import with_progress_bar
from corehq.util.markup import shell_green, shell_red

log = logging.getLogger('main_couch_sql_datamigration')

# Script action constants
MIGRATE = "MIGRATE"
COMMIT = "COMMIT"
RESET = "reset"  # was --blow-away
REWIND = "rewind"
STATS = "stats"
DIFF = "diff"

CASE_DIFF = {"process": True, "local": False, "none": None}


class Command(BaseCommand):
    help = """
    Step 1: Run 'MIGRATE'
    Step 2a: If diffs, use 'diff' to view diffs
    Step 2b: Use 'stats --verbose' to view more stats output
    Step 3: If no diffs or diffs are acceptable run 'COMMIT'
    Step 4: Run 'reset' to abort the current migration
    """

    def add_arguments(self, parser):
        parser.add_argument('domain')
        parser.add_argument('action', choices=[
            MIGRATE,
            COMMIT,
            RESET,
            REWIND,
            STATS,
            DIFF,
        ])
        parser.add_argument('--no-input', action='store_true', default=False)
        parser.add_argument('--debug', action='store_true', default=False)
        parser.add_argument('--verbose', action='store_true', default=False,
            help="Show verbose stats output.")
        parser.add_argument('--state-dir',
            default=os.environ.get("CCHQ_MIGRATION_STATE_DIR"),
            required="CCHQ_MIGRATION_STATE_DIR" not in os.environ,
            help="""
                Directory for couch2sql logs and migration state. This must not
                reside on an NFS volume for migration state consistency.
                Can be set in environment: CCHQ_MIGRATION_STATE_DIR
            """)
        parser.add_argument('--live',
            dest="live_migrate", action='store_true', default=False,
            help='''
                Do migration in a way that will not be seen by
                `any_migrations_in_progress(...)` so it does not block
                operations like syncs, form submissions, sms activity,
                etc. A "live" migration will stop when it encounters a
                form that has been submitted within an hour of the
                current time. Live migrations can be resumed after
                interruption or to top off a previous live migration by
                processing unmigrated forms that are older than one
                hour. Migration state must be present in the state
                directory to resume. A live migration may be followed by
                a normal (non-live) migration, which will commit the
                result if all goes well.
            ''')
        parser.add_argument('--rebuild-state',
            dest="rebuild_state", action='store_true', default=False,
            help="""
                Rebuild migration state by restarting form iterations.
                Check each form to see if it has already been migrated,
                and enqueue unmigrated forms as necessary until the
                iteration reaches the place where it had previously
                stopped. Finally resume the migration as usual. Forms
                (and associated cases) encountered while rebuilding
                state will not be diffed unless the form is not found
                in SQL, which means that some cases that were previously
                queued to diff may not be diffed.
            """)
        parser.add_argument('--case-diff',
            dest='case_diff', default="process",
            choices=["process", "local", "none"],
            help='''
                process: diff cases in a separate process (default).
                local: diff cases in the migration process.
                none: do not diff cases.
            ''')
        parser.add_argument('--forms', default=None,
            help="""
                Migrate specific forms. The value of this option should
                be a space-delimited list of form ids OR 'skipped' to
                migrate forms skipped by previous migration.
            """)
        parser.add_argument('-x', '--stop-on-error',
            dest="stop_on_error", action='store_true', default=False,
            help="""
                Stop on form migration error rather than recording diffs
                and continuing with next form.
            """)
        parser.add_argument('--to', dest="rewind", help="Rewind iteration state.")

    def handle(self, domain, action, **options):
        if should_use_sql_backend(domain):
            raise CommandError('It looks like {} has already been migrated.'.format(domain))

        for opt in [
            "no_input",
            "verbose",
            "state_dir",
            "live_migrate",
            "case_diff",
            "rebuild_state",
            "stop_on_error",
            "forms",
            "rewind",
        ]:
            setattr(self, opt, options[opt])

        if self.no_input and not settings.UNIT_TESTING:
            raise CommandError('--no-input only allowed for unit testing')
        if action != MIGRATE and self.live_migrate:
            raise CommandError("--live only allowed with `MIGRATE`")
        if action != MIGRATE and self.rebuild_state:
            raise CommandError("--rebuild-state only allowed with `MIGRATE`")
        if action != MIGRATE and self.forms:
            raise CommandError("--forms only allowed with `MIGRATE`")
        if action != MIGRATE and self.stop_on_error:
            raise CommandError("--stop-on-error only allowed with `MIGRATE`")
        if action != STATS and self.verbose:
            raise CommandError("--verbose only allowed for `stats`")
        if action != REWIND and self.rewind:
            raise CommandError("--to=... only allowed for `rewind`")

        assert Domain.get_by_name(domain), f'Unknown domain "{domain}"'
        slug = f"{action.lower()}-{domain}"
        setup_logging(self.state_dir, slug, options['debug'])
        getattr(self, "do_" + action)(domain)

    def do_MIGRATE(self, domain):
        set_couch_sql_migration_started(domain, self.live_migrate)
        do_couch_to_sql_migration(
            domain,
            self.state_dir,
            with_progress=not self.no_input,
            live_migrate=self.live_migrate,
            diff_process=CASE_DIFF[self.case_diff],
            rebuild_state=self.rebuild_state,
            stop_on_error=self.stop_on_error,
            forms=self.forms,
        )

        return_code = 0
        if self.live_migrate:
            print("Live migration completed.")
            has_diffs = True
        else:
            has_diffs = self.print_stats(domain, short=True, diffs_only=True)
            return_code = int(has_diffs)
        if has_diffs:
            print("\nRun `diff` or `stats [--verbose]` for more details.\n")
        if return_code:
            sys.exit(return_code)

    def do_reset(self, domain):
        if not self.no_input:
            _confirm(
                "This will delete all SQL forms and cases for the domain {}. "
                "Are you sure you want to continue?".format(domain)
            )
        set_couch_sql_migration_not_started(domain)
        blow_away_migration(domain, self.state_dir)

    def do_COMMIT(self, domain):
        if not couch_sql_migration_in_progress(domain, include_dry_runs=False):
            raise CommandError("cannot commit a migration that is not in state in_progress")
        if not self.no_input:
            _confirm(
                "This will convert the domain to use the SQL backend and"
                "allow new form submissions to be processed. "
                "Are you sure you want to do this for domain '{}'?".format(domain)
            )
        set_couch_sql_migration_complete(domain)

    def do_stats(self, domain):
        self.print_stats(domain, short=not self.verbose)

    def do_diff(self, domain):
        db = open_state_db(domain, self.state_dir)
        diffs = sorted(db.get_diffs(), key=lambda d: d.kind)
        for doc_type, diffs in groupby(diffs, key=lambda d: d.kind):
            print('-' * 50, "Diffs for {}".format(doc_type), '-' * 50)
            for diff in diffs:
                print('[{}({})] {}'.format(doc_type, diff.doc_id, diff.json_diff))

    def do_rewind(self, domain):
        db = open_state_db(domain, self.state_dir)
        assert os.path.exists(db.db_filepath), db.db_filepath
        db = init_state_db(domain, self.state_dir)
        rewind_iteration_state(db, domain, self.rewind)

    def print_stats(self, domain, short=True, diffs_only=False):
        status = get_couch_sql_migration_status(domain)
        print("Couch to SQL migration status for {}: {}".format(domain, status))
        db = open_state_db(domain, self.state_dir)
        try:
            diff_stats = db.get_diff_stats()
        except OperationalError:
            diff_stats = {}

        has_diffs = False
        for doc_type in doc_types():
            form_ids_in_couch = set(get_form_ids_by_type(domain, doc_type))
            if doc_type == "XFormInstance":
                form_ids_in_couch.update(get_doc_ids_in_domain_by_type(
                    domain, "HQSubmission", XFormInstance.get_db()))
            form_ids_in_sql = set(FormAccessorSQL.get_form_ids_in_domain_by_type(domain, doc_type))
            diff_count, num_docs_with_diffs = diff_stats.pop(doc_type, (0, 0))
            has_diffs |= self._print_status(
                doc_type, form_ids_in_couch, form_ids_in_sql, diff_count, num_docs_with_diffs, short, diffs_only
            )

        form_ids_in_couch = set(get_doc_ids_in_domain_by_type(
            domain, "XFormInstance-Deleted", XFormInstance.get_db())
        )
        form_ids_in_sql = set(FormAccessorSQL.get_deleted_form_ids_in_domain(domain))
        diff_count, num_docs_with_diffs = diff_stats.pop("XFormInstance-Deleted", (0, 0))
        has_diffs |= self._print_status(
            "XFormInstance-Deleted", form_ids_in_couch, form_ids_in_sql,
            diff_count, num_docs_with_diffs, short, diffs_only
        )

        ZERO = Counts(0, 0)
        if db.has_doc_counts():
            doc_counts = db.get_doc_counts()
            couch_missing_cases = doc_counts.get("CommCareCase-couch", ZERO).missing
        else:
            doc_counts = None
            couch_missing_cases = 0
        for doc_type in CASE_DOC_TYPES:
            if doc_counts is not None:
                counts = doc_counts.get(doc_type, ZERO)
                case_ids_in_couch = db.get_missing_doc_ids(doc_type) if counts.missing else set()
                case_ids_in_sql = counts
            elif doc_type == "CommCareCase":
                case_ids_in_couch = set(get_case_ids_in_domain(domain))
                case_ids_in_sql = set(CaseAccessorSQL.get_case_ids_in_domain(domain))
            elif doc_type == "CommCareCase-Deleted":
                case_ids_in_couch = set(get_doc_ids_in_domain_by_type(
                    domain, "CommCareCase-Deleted", XFormInstance.get_db())
                )
                case_ids_in_sql = set(CaseAccessorSQL.get_deleted_case_ids_in_domain(domain))
            else:
                raise NotImplementedError(doc_type)
            diff_count, num_docs_with_diffs = diff_stats.pop(doc_type, (0, 0))
            has_diffs |= self._print_status(
                doc_type,
                case_ids_in_couch,
                case_ids_in_sql,
                diff_count,
                num_docs_with_diffs,
                short,
                diffs_only,
            )
            if doc_type == "CommCareCase" and couch_missing_cases:
                has_diffs = True
                print(shell_red("%s cases could not be loaded from Couch" % couch_missing_cases))
                if not short:
                    for case_id in db.get_missing_doc_ids("CommCareCase-couch"):
                        print(case_id)

        if diff_stats:
            for key, counts in diff_stats.items():
                diff_count, num_docs_with_diffs = counts
                has_diffs |= self._print_status(
                    key, set(), set(), diff_count, num_docs_with_diffs, short, diffs_only
                )

        if diffs_only and not has_diffs:
            print(shell_green("No differences found between old and new docs!"))
        return has_diffs

    def _print_status(self, name, ids_in_couch, ids_in_sql, diff_count, num_docs_with_diffs, short, diffs_only):
        if isinstance(ids_in_sql, Counts):
            counts, ids_in_sql = ids_in_sql, set()
            assert len(ids_in_couch) == counts.missing, (len(ids_in_couch), counts.missing)
            n_couch = counts.total
            n_sql = counts.total - counts.missing
        else:
            n_couch = len(ids_in_couch)
            n_sql = len(ids_in_sql)
        has_diff = ids_in_couch != ids_in_sql or diff_count

        if diffs_only and not has_diff:
            return False

        def _highlight(text):
            return shell_red(text) if has_diff else text

        row = "{:^38} {} {:^38}"
        sep = "|" if ids_in_couch == ids_in_sql else "≠"
        doc_count_row = row.format(n_couch, sep, n_sql)

        print('\n{:_^79}'.format(" %s " % name))
        print(row.format('Couch', '|', 'SQL'))
        print(_highlight(doc_count_row))
        if diff_count:
            print(_highlight("{:^83}".format('{} diffs ({} docs)'.format(diff_count, num_docs_with_diffs))))

        if not short:
            if ids_in_couch ^ ids_in_sql:
                couch_only = list(ids_in_couch - ids_in_sql)
                sql_only = list(ids_in_sql - ids_in_couch)
                for couch, sql in zip_longest(couch_only, sql_only):
                    print(row.format(couch or '', '|', sql or ''))

        return True


def _confirm(message):
    response = input('{} [y/N]'.format(message)).lower()
    if response != 'y':
        raise CommandError('abort')


def blow_away_migration(domain, state_dir):
    assert not should_use_sql_backend(domain)
    delete_state_db(domain, state_dir)

    log.info("deleting forms...")
    for form_ids in iter_chunks(XFormInstanceSQL, "form_id", domain):
        FormAccessorSQL.hard_delete_forms(domain, form_ids, delete_attachments=False)

    log.info("deleting cases...")
    for case_ids in iter_chunks(CommCareCaseSQL, "case_id", domain):
        CaseAccessorSQL.hard_delete_cases(domain, case_ids)

    log.info("blew away migration for domain %s\n", domain)


def iter_chunks(model_class, field, domain, chunk_size=5000):
    where = Q(domain=domain)
    row_count = estimate_partitioned_row_count(model_class, where)
    rows = paginate_query_across_partitioned_databases(
        model_class,
        where,
        values=[field],
        load_source='couch_to_sql_migration',
        query_size=chunk_size,
    )
    values = (r[0] for r in rows)
    values = with_progress_bar(values, row_count, oneline="concise")
    yield from chunked(values, chunk_size, list)
