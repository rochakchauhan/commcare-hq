from dateutil.relativedelta import relativedelta

from custom.icds_reports.const import AGG_CCS_RECORD_BP_TABLE
from custom.icds_reports.utils.aggregation_helpers import month_formatter
from custom.icds_reports.utils.aggregation_helpers.distributed.base import (
    StateBasedAggregationDistributedHelper,
)


class BirthPreparednessFormsAggregationDistributedHelper(StateBasedAggregationDistributedHelper):
    helper_key = 'birth-preparedness-forms'
    ucr_data_source_id = 'static-dashboard_birth_preparedness_forms'
    aggregate_parent_table = AGG_CCS_RECORD_BP_TABLE

    def data_from_ucr_query(self):
        current_month_start = month_formatter(self.month)
        next_month_start = month_formatter(self.month + relativedelta(months=1))

        return """
        SELECT DISTINCT ccs_record_case_id AS case_id,
        supervisor_id,
        %(current_month_start)s::date as month,
        LAST_VALUE(timeend) OVER w AS latest_time_end,
        MAX(immediate_breastfeeding) OVER w AS immediate_breastfeeding,
        MAX(play_birth_preparedness_vid) OVER w as play_birth_preparedness_vid,
        MAX(counsel_preparation) OVER w as counsel_preparation,
        MAX(play_family_planning_vid) OVER w as play_family_planning_vid,
        MAX(conceive) OVER w as conceive,
        MAX(counsel_accessible_ppfp) OVER w as counsel_accessible_ppfp,
        LAST_VALUE(eating_extra) OVER w as eating_extra,
        LAST_VALUE(resting) OVER w as resting,
        LAST_VALUE(anc_weight) OVER w as anc_weight,
        LAST_VALUE(anc_blood_pressure) OVER w as anc_blood_pressure,
        LAST_VALUE(bp_sys) OVER w as bp_sys,
        LAST_VALUE(bp_dia) OVER w as bp_dia,
        LAST_VALUE(anc_hemoglobin) OVER w as anc_hemoglobin,
        LAST_VALUE(bleeding) OVER w as bleeding,
        LAST_VALUE(swelling) OVER w as swelling,
        LAST_VALUE(blurred_vision) OVER w as blurred_vision,
        LAST_VALUE(convulsions) OVER w as convulsions,
        LAST_VALUE(rupture) OVER w as rupture,
        LAST_VALUE(anemia) OVER w as anemia,
        LAST_VALUE(anc_abnormalities) OVER w as anc_abnormalities,
        LAST_VALUE(using_ifa) OVER w as using_ifa,
        GREATEST(LAST_VALUE(ifa_last_seven_days) OVER w, 0) as ifa_last_seven_days,
        SUM(CASE WHEN
            (unscheduled_visit=0 AND days_visit_late < 8) OR (timeend::DATE - next_visit) < 8
            THEN 1 ELSE 0 END
        ) OVER w as valid_visits
        FROM "{ucr_tablename}"
        WHERE timeend >= %(current_month_start)s AND timeend < %(next_month_start)s AND state_id = %(state_id)s
        WINDOW w AS (
            PARTITION BY supervisor_id, ccs_record_case_id
            ORDER BY timeend RANGE BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
        )
        """.format(ucr_tablename=self.ucr_tablename), {
            "current_month_start": current_month_start,
            "next_month_start": next_month_start,
            "state_id": self.state_id
        }

    def aggregation_query(self):
        month = self.month.replace(day=1)
        previous_month = month - relativedelta(months=1)

        ucr_query, ucr_query_params = self.data_from_ucr_query()
        query_params = {
            "month": month_formatter(month),
            "state_id": self.state_id,
            "previous_month": previous_month
        }
        query_params.update(ucr_query_params)

        return """
        INSERT INTO "{tablename}" (
          state_id, supervisor_id, month, case_id, latest_time_end_processed,
          immediate_breastfeeding, anemia, eating_extra, resting,
          anc_weight, anc_blood_pressure, bp_sys, bp_dia, anc_hemoglobin,
          bleeding, swelling, blurred_vision, convulsions, rupture, anc_abnormalities, valid_visits,
          play_birth_preparedness_vid, counsel_preparation, play_family_planning_vid, conceive,
          counsel_accessible_ppfp, ifa_last_seven_days,using_ifa
        ) (
          SELECT
            %(state_id)s AS state_id,
            COALESCE(ucr.supervisor_id, prev_month.supervisor_id) AS supervisor_id,
            %(month)s AS month,
            COALESCE(ucr.case_id, prev_month.case_id) AS case_id,
            COALESCE(ucr.latest_time_end, prev_month.latest_time_end_processed) AS latest_time_end_processed,
            GREATEST(ucr.immediate_breastfeeding, prev_month.immediate_breastfeeding) AS immediate_breastfeeding,
            COALESCE(ucr.anemia,prev_month.anemia) AS anemia,
            COALESCE(ucr.eating_extra,prev_month.eating_extra) AS eating_extra,
            COALESCE(ucr.resting,prev_month.resting) AS resting,
            COALESCE(ucr.anc_weight,prev_month.anc_weight) anc_weight,
            COALESCE(ucr.anc_blood_pressure,prev_month.anc_blood_pressure) as anc_blood_pressure,
            COALESCE(ucr.bp_sys,prev_month.bp_sys) as bp_sys,
            COALESCE(ucr.bp_dia,prev_month.bp_dia) as bp_dia,
            COALESCE(ucr.anc_hemoglobin,prev_month.anc_hemoglobin) as anc_hemoglobin,
            COALESCE(ucr.bleeding,prev_month.bleeding) as bleeding,
            COALESCE(ucr.swelling,prev_month.swelling) as swelling,
            COALESCE(ucr.blurred_vision,prev_month.blurred_vision) as blurred_vision,
            COALESCE(ucr.convulsions,prev_month.convulsions) as convulsions,
            COALESCE(ucr.rupture,prev_month.rupture) as rupture,
            COALESCE(ucr.anc_abnormalities,prev_month.anc_abnormalities) as anc_abnormalities,
            COALESCE(ucr.valid_visits, 0) as valid_visits,
            GREATEST(
                ucr.play_birth_preparedness_vid, prev_month.play_birth_preparedness_vid
            ) as play_birth_preparedness_vid,
            GREATEST(ucr.counsel_preparation, prev_month.counsel_preparation) as counsel_preparation,
            GREATEST(
                ucr.play_family_planning_vid, prev_month.play_family_planning_vid
            ) as play_family_planning_vid,
            GREATEST(ucr.conceive,prev_month.conceive) as conceive,
            GREATEST(ucr.counsel_accessible_ppfp, prev_month.counsel_accessible_ppfp) as counsel_accessible_ppfp,
            COALESCE(ucr.ifa_last_seven_days, prev_month.ifa_last_seven_days) as ifa_last_seven_days,
            COALESCE(ucr.using_ifa, prev_month.using_ifa) as using_ifa
          FROM ({ucr_table_query}) ucr
          FULL OUTER JOIN (
             SELECT * FROM "{tablename}" WHERE month = %(previous_month)s AND state_id = %(state_id)s
          ) prev_month
          ON ucr.case_id = prev_month.case_id AND ucr.supervisor_id = prev_month.supervisor_id
          WHERE coalesce(ucr.month, %(month)s) = %(month)s
            AND coalesce(prev_month.month, %(previous_month)s) = %(previous_month)s
            AND coalesce(prev_month.state_id, %(state_id)s) = %(state_id)s
        )
        """.format(
            ucr_table_query=ucr_query,
            tablename=self.aggregate_parent_table
        ), query_params
