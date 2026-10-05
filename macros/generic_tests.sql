{% macro surrogate_key(field_list) %}
    {#
        Deterministic hash key across the supplied fields.

        Values are coalesced to a sentinel before hashing so that a null and
        the string 'null' do not collide, and separated by a delimiter that
        cannot appear in the inputs so that ('a', 'bc') and ('ab', 'c') produce
        different keys.

        Implemented here rather than pulled from dbt_utils to keep the project
        free of package dependencies: it clones and runs with nothing but the
        requirements file.
    #}
    md5(
        {%- for field in field_list %}
        coalesce(cast({{ field }} as varchar), '_dbt_null_')
        {%- if not loop.last %} || '|-|' || {% endif -%}
        {%- endfor %}
    )
{% endmacro %}


{% test accepted_range(model, column_name, min_value=none, max_value=none, inclusive=true) %}
    {#
        Fails rows falling outside the given bounds. Nulls pass: absence is
        tested with not_null where it matters, and failing nulls here would
        make every optional measure untestable.
    #}
    {%- set lower = '>=' if inclusive else '>' -%}
    {%- set upper = '<=' if inclusive else '<' -%}

    select
        {{ column_name }} as failing_value,
        count(*) as failing_rows
    from {{ model }}
    where {{ column_name }} is not null
      and (
          {%- if min_value is not none %}
          not ({{ column_name }} {{ lower }} {{ min_value }})
          {%- endif %}
          {%- if min_value is not none and max_value is not none %} or {% endif %}
          {%- if max_value is not none %}
          not ({{ column_name }} {{ upper }} {{ max_value }})
          {%- endif %}
      )
    group by 1

{% endtest %}
