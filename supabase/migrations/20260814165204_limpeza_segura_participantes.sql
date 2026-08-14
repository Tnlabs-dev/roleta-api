set lock_timeout = '5s';
set statement_timeout = '30s';

create or replace function public.limpar_participantes_admin(
    p_campanha_id integer,
    p_total_esperado integer
)
returns table (
    resultado text,
    quantidade_excluida integer
)
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    v_total_atual integer;
    v_total_excluido integer;
begin
    if p_total_esperado is null or p_total_esperado < 0 then
        raise exception 'p_total_esperado deve ser maior ou igual a zero';
    end if;

    lock table public.participantes in share row exclusive mode;

    if not exists (
        select 1
          from public.campanhas as c
         where c.id = p_campanha_id
    ) then
        return query select 'campanha_nao_encontrada'::text, 0;
        return;
    end if;

    select count(*)::integer
      into v_total_atual
      from public.participantes as pt
     where pt.campanha_id = p_campanha_id;

    if v_total_atual <> p_total_esperado then
        return query select 'dados_alterados'::text, v_total_atual;
        return;
    end if;

    delete from public.participantes as pt
     where pt.campanha_id = p_campanha_id;

    get diagnostics v_total_excluido = row_count;

    insert into public.admin_auditoria (
        campanha_id,
        acao,
        entidade,
        entidade_id,
        detalhes
    ) values (
        p_campanha_id,
        'Participantes excluidos com backup',
        'campanha',
        p_campanha_id,
        jsonb_build_object(
            'quantidade_excluida', v_total_excluido,
            'total_confirmado_no_backup', p_total_esperado
        )
    );

    return query select 'sucesso'::text, v_total_excluido;
end;
$$;

revoke all on function public.limpar_participantes_admin(integer, integer)
from public, anon, authenticated;

grant execute on function public.limpar_participantes_admin(integer, integer)
to service_role;

comment on function public.limpar_participantes_admin(integer, integer) is
'Exclui participantes de uma campanha somente quando o total atual coincide com o backup confirmado pela administracao.';
