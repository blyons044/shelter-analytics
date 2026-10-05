{% macro classify_outcome(column_name) %}
    {#
        Groups outcome types into live, non-live, and unknown.

        The split follows the Shelter Animals Count basic matrix. 'Missing' is
        treated as non-live rather than excluded: an animal unaccounted for is
        not a live release, and classifying it as unknown would quietly inflate
        the rate.

        Returning 'Unknown' for unrecognised values rather than defaulting to
        one side means a new outcome type appearing in the feed shows up in the
        data instead of silently changing the metric. The accepted_values test
        on this column is what surfaces it.
    #}
    case
        when {{ column_name }} is null then null
        when {{ column_name }} in ('Adoption', 'Return To Owner', 'Rto-Adopt', 'Transfer')
            then 'Live'
        when {{ column_name }} in ('Euthanasia', 'Died', 'Disposal', 'Missing')
            then 'Non-Live'
        else 'Unknown'
    end
{% endmacro %}


{% macro live_release_rate(class_column, exclude_flag_column=none) %}
    {#
        Live releases over all closed outcomes.

        Passing exclude_flag_column removes those rows from both numerator and
        denominator, which is how the adjusted rate excludes owner-requested
        euthanasia.

        nullif on the denominator returns null rather than raising when a
        grouping has no closed outcomes, so a sparse month reads as 'no data'
        instead of zero percent.
    #}
    {%- set guard = "and not " ~ exclude_flag_column if exclude_flag_column else "" -%}
    round(
        100.0 * count(*) filter (where {{ class_column }} = 'Live' {{ guard }})
        / nullif(count(*) filter (where {{ class_column }} in ('Live', 'Non-Live') {{ guard }}), 0)
    , 2)
{% endmacro %}
