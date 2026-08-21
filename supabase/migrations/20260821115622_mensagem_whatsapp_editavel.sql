alter table public.campanhas
    add column if not exists mensagem_whatsapp text;

do $$
begin
    if not exists (
        select 1
          from pg_constraint
         where conname = 'campanhas_mensagem_whatsapp_valida'
           and conrelid = 'public.campanhas'::regclass
    ) then
        alter table public.campanhas
            add constraint campanhas_mensagem_whatsapp_valida
            check (
                mensagem_whatsapp is null
                or char_length(btrim(mensagem_whatsapp)) between 10 and 1000
            ) not valid;
    end if;
end
$$;

alter table public.campanhas
    validate constraint campanhas_mensagem_whatsapp_valida;

create or replace function public.criar_campanha_admin(
    p_nome text,
    p_data_inicio timestamptz,
    p_data_fim timestamptz,
    p_texto_consentimento text,
    p_mensagem_whatsapp text
)
returns table (resultado text, campanha_id integer)
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    v_campanha_id integer;
begin
    insert into public.campanhas (
        nome,
        status,
        data_inicio,
        data_fim,
        texto_consentimento,
        mensagem_whatsapp
    ) values (
        btrim(p_nome),
        'rascunho',
        p_data_inicio,
        p_data_fim,
        btrim(p_texto_consentimento),
        nullif(btrim(p_mensagem_whatsapp), '')
    )
    returning id into v_campanha_id;

    insert into public.admin_auditoria (
        campanha_id, acao, entidade, entidade_id, detalhes
    ) values (
        v_campanha_id, 'Campanha criada', 'campanha', v_campanha_id,
        jsonb_build_object('nome', btrim(p_nome), 'status', 'rascunho')
    );

    return query select 'sucesso', v_campanha_id;
end;
$$;

create or replace function public.atualizar_campanha_admin(
    p_campanha_id integer,
    p_nome text,
    p_status text,
    p_data_inicio timestamptz,
    p_data_fim timestamptz,
    p_texto_consentimento text,
    p_mensagem_whatsapp text
)
returns table (resultado text, campanha_id integer)
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    v_status_anterior text;
    v_mensagem_anterior text;
begin
    select c.status, c.mensagem_whatsapp
      into v_status_anterior, v_mensagem_anterior
      from public.campanhas as c
     where c.id = p_campanha_id
     for update;

    if not found then
        return query select 'nao_encontrada', null::integer;
        return;
    end if;

    if p_status = 'ativa' then
        if exists (
            select 1 from public.campanhas as c
             where c.status = 'ativa' and c.id <> p_campanha_id
        ) then
            return query select 'outra_campanha_ativa', p_campanha_id;
            return;
        end if;

        if (
            select count(*) from public.premios as p
             where p.campanha_id = p_campanha_id and p.ativo = true
        ) < 2 then
            return query select 'premios_insuficientes', p_campanha_id;
            return;
        end if;
    end if;

    update public.campanhas
       set nome = btrim(p_nome),
           status = p_status,
           data_inicio = p_data_inicio,
           data_fim = p_data_fim,
           texto_consentimento = btrim(p_texto_consentimento),
           mensagem_whatsapp = coalesce(
               nullif(btrim(p_mensagem_whatsapp), ''),
               mensagem_whatsapp
           )
     where id = p_campanha_id;

    insert into public.admin_auditoria (
        campanha_id, acao, entidade, entidade_id, detalhes
    ) values (
        p_campanha_id, 'Campanha atualizada', 'campanha', p_campanha_id,
        jsonb_build_object(
            'nome', btrim(p_nome),
            'status_anterior', v_status_anterior,
            'status_novo', p_status,
            'mensagem_whatsapp_alterada',
            v_mensagem_anterior is distinct from coalesce(
                nullif(btrim(p_mensagem_whatsapp), ''),
                v_mensagem_anterior
            )
        )
    );

    return query select 'sucesso', p_campanha_id;
end;
$$;

revoke all on function public.criar_campanha_admin(
    text, timestamptz, timestamptz, text, text
) from public, anon, authenticated;
revoke all on function public.atualizar_campanha_admin(
    integer, text, text, timestamptz, timestamptz, text, text
) from public, anon, authenticated;

grant execute on function public.criar_campanha_admin(
    text, timestamptz, timestamptz, text, text
) to service_role;
grant execute on function public.atualizar_campanha_admin(
    integer, text, text, timestamptz, timestamptz, text, text
) to service_role;

comment on column public.campanhas.mensagem_whatsapp is
'Mensagem de compartilhamento da campanha, editável pela área administrativa.';
