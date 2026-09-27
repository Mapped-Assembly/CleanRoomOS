"""Validate and summarize local fixture inputs without running a workflow."""
import argparse
import json
from pathlib import Path

from cleanroom_os.adapters import FileInputAdapter, load_context


def main() -> None:
    """Expose an offline smoke check with explicit scenario selection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--resolved', action='store_true', help='Select human-supplied schedule version 2')
    parser.add_argument('--normal-results', action='store_true', help='Select complete normal LIMS dataset')
    args = parser.parse_args()
    adapter = FileInputAdapter(args.directory)
    context = load_context(adapter, resolved=args.resolved)
    results = adapter.load_lims(anomalies=not args.normal_results)
    print(json.dumps(dict(synthetic=True, schedule_version=context.schedule.version,
                         rooms=[r.room_id for r in context.rooms],
                         rooms_without_access=[a.room_id for a in context.schedule.availability if not a.windows],
                         required_samples={r.room_id:r.count for r in context.recipe.requirements},
                         lims_record_count=len(results),
                         lims_plan_revisions=sorted({r.plan_revision for r in results}),
                         note='Inputs only. LIMS fixtures target resolved plan revision 2; loading does not approve or execute a plan.'), indent=2))


if __name__ == '__main__':
    main()
