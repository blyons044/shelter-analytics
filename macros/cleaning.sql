{% macro clean_label(column_name) %}
    {#
        Categorical fields arrive with inconsistent casing and stray padding.
        Normalising once here keeps every downstream group-by honest; without
        it, 'Stray', 'STRAY' and ' Stray ' count as three intake types.
    #}
    {#
        DuckDB has no initcap, and title casing has to survive hyphens: the
        feed carries 'Rto-Adopt', which a naive word-split would render as
        'Rto-adopt' and quietly split one outcome type into two.
    #}
    nullif(
        array_to_string(
            list_transform(
                string_split(
                    regexp_replace(lower(trim({{ column_name }})), '\s+', ' ', 'g'),
                    ' '
                ),
                word -> array_to_string(
                    list_transform(
                        string_split(word, '-'),
                        part -> upper(substr(part, 1, 1)) || substr(part, 2)
                    ),
                    '-'
                )
            ),
            ' '
        ),
        ''
    )
{% endmacro %}


{% macro age_string_to_days(column_name) %}
    {#
        Ages arrive as free text: '2 years', '3 months', '1 week', '4 days',
        occasionally the literal string 'NULL', and occasionally a negative
        value where a date of birth was entered after the event.

        Returns a day count, or null where the value cannot be trusted.
        Negative ages are nulled rather than made absolute: a negative age is
        evidence the record is wrong, not evidence of its magnitude.
    #}
    case
        when {{ column_name }} is null then null
        when upper(trim({{ column_name }})) in ('NULL', '', 'UNKNOWN') then null
        when try_cast(regexp_extract(trim({{ column_name }}), '^(-?\d+)', 1) as integer) is null then null
        when try_cast(regexp_extract(trim({{ column_name }}), '^(-?\d+)', 1) as integer) < 0 then null
        else try_cast(regexp_extract(trim({{ column_name }}), '^(-?\d+)', 1) as integer)
             * case
                   when lower({{ column_name }}) like '%year%' then 365
                   when lower({{ column_name }}) like '%month%' then 30
                   when lower({{ column_name }}) like '%week%' then 7
                   when lower({{ column_name }}) like '%day%' then 1
                   else null
               end
    end
{% endmacro %}
