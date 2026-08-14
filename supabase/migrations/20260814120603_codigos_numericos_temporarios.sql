set lock_timeout = '5s';
set statement_timeout = '30s';

alter table public.acessos_roleta
    add column if not exists codigo_curto text,
    add column if not exists expira_em timestamptz;

update public.acessos_roleta
   set expira_em = case
       when utilizado_em is null and cancelado_em is null
           then clock_timestamp() + interval '30 minutes'
       else data_criacao + interval '30 minutes'
   end
 where expira_em is null;

alter table public.acessos_roleta
    alter column expira_em set default (now() + interval '30 minutes'),
    alter column expira_em set not null;

do $$
begin
    if not exists (
        select 1 from pg_constraint
         where conrelid = 'public.acessos_roleta'::regclass
           and conname = 'acessos_roleta_codigo_curto_formato_check'
    ) then
        alter table public.acessos_roleta
            add constraint acessos_roleta_codigo_curto_formato_check
            check (codigo_curto is null or codigo_curto ~ '^[0-9]{6}$');
    end if;

    if not exists (
        select 1 from pg_constraint
         where conrelid = 'public.acessos_roleta'::regclass
           and conname = 'acessos_roleta_expiracao_check'
    ) then
        alter table public.acessos_roleta
            add constraint acessos_roleta_expiracao_check
            check (expira_em > data_criacao);
    end if;
end;
$$;

do $$
begin
    if not exists (
        select 1
          from pg_index as i
          join pg_class as tabela on tabela.oid = i.indrelid
          join pg_namespace as esquema on esquema.oid = tabela.relnamespace
         where esquema.nspname = 'public'
           and tabela.relname = 'acessos_roleta'
           and i.indisunique
           and i.indnkeyatts = 1
           and i.indkey[0] = (
               select attnum
                 from pg_attribute
                where attrelid = 'public.acessos_roleta'::regclass
                  and attname = 'token'
           )
    ) then
        create unique index acessos_roleta_token_unico_idx
            on public.acessos_roleta (token);
    end if;
end;
$$;

create unique index if not exists acessos_roleta_codigo_campanha_unico_idx
    on public.acessos_roleta (campanha_id, codigo_curto)
    where codigo_curto is not null;

create index if not exists acessos_roleta_expiracao_pendente_idx
    on public.acessos_roleta (expira_em)
    where utilizado_em is null and cancelado_em is null;

create or replace function public.gerar_convite_roleta(
    p_token text,
    p_codigo_curto text,
    p_expira_em timestamptz
)
returns table (
    resultado text,
    mensagem text,
    codigo_gerado text,
    campanha text,
    expira_em timestamptz
)
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    v_campanha_id integer;
    v_campanha_nome text;
    v_acesso_id integer;
begin
    if p_token !~ '^[A-Za-z0-9_-]{16,64}$'
       or p_codigo_curto !~ '^[0-9]{6}$'
       or p_expira_em <= now()
       or p_expira_em > now() + interval '121 minutes' then
        raise exception using
            errcode = '22023',
            message = 'Dados inválidos para gerar o código.';
    end if;

    select c.id, c.nome
      into v_campanha_id, v_campanha_nome
      from public.campanhas as c
     where c.status = 'ativa'
       and (c.data_inicio is null or c.data_inicio <= now())
       and (c.data_fim is null or c.data_fim > now())
     limit 1;

    if v_campanha_id is null then
        return query
        select 'sem_campanha', 'Não existe uma campanha ativa.',
               null::text, null::text, null::timestamptz;
        return;
    end if;

    insert into public.acessos_roleta (
        campanha_id, token, codigo_curto, expira_em
    ) values (
        v_campanha_id, p_token, p_codigo_curto, p_expira_em
    )
    on conflict do nothing
    returning id into v_acesso_id;

    if v_acesso_id is null then
        return query
        select 'codigo_em_uso', 'O código sorteado já está em uso.',
               null::text, v_campanha_nome, null::timestamptz;
        return;
    end if;

    return query
    select 'sucesso', null::text, p_codigo_curto,
           v_campanha_nome, p_expira_em;
end;
$$;

drop function public.verificar_token_roleta(text);

create function public.verificar_token_roleta(p_token text)
returns table (
    valido boolean,
    mensagem text,
    campanha text,
    expira_em timestamptz
)
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    v_utilizado_em timestamptz;
    v_cancelado_em timestamptz;
    v_expira_em timestamptz;
    v_status text;
    v_data_inicio timestamptz;
    v_data_fim timestamptz;
    v_campanha_nome text;
begin
    select a.utilizado_em, a.cancelado_em, a.expira_em,
           c.status, c.data_inicio, c.data_fim, c.nome
      into v_utilizado_em, v_cancelado_em, v_expira_em,
           v_status, v_data_inicio, v_data_fim, v_campanha_nome
     from public.acessos_roleta as a
     join public.campanhas as c on c.id = a.campanha_id
     where a.codigo_curto = p_token or a.token = p_token
     order by a.data_criacao desc
     limit 1;

    if not found then
        return query
        select false, 'Código não existe!', null::text, null::timestamptz;
    elsif v_cancelado_em is not null then
        return query
        select false, 'Este código foi cancelado.', v_campanha_nome, v_expira_em;
    elsif v_utilizado_em is not null then
        return query
        select false, 'Este código já foi utilizado!', v_campanha_nome, v_expira_em;
    elsif v_expira_em <= now() then
        return query
        select false, 'Este código expirou. Peça um novo código no caixa.',
               v_campanha_nome, v_expira_em;
    elsif v_status <> 'ativa'
       or (v_data_inicio is not null and v_data_inicio > now())
       or (v_data_fim is not null and v_data_fim <= now()) then
        return query
        select false, 'Esta campanha não está ativa.', v_campanha_nome, v_expira_em;
    else
        return query select true, null::text, v_campanha_nome, v_expira_em;
    end if;
end;
$$;

create or replace function public.sortear_premio_com_privacidade(
    p_token text,
    p_nome text,
    p_whatsapp text,
    p_ciencia_privacidade boolean,
    p_data_nascimento date,
    p_consentimento_aniversario boolean
)
returns table (
    resultado text,
    mensagem text,
    premio text,
    indice_roleta integer,
    participante_id integer
)
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
    v_acesso_id integer;
    v_campanha_id integer;
    v_texto_ciencia_privacidade text;
    v_politica_privacidade_versao text;
    v_premio_id integer;
    v_premio_nome text;
    v_total_pesos numeric;
    v_alvo numeric;
    v_acumulado numeric := 0;
    v_participante_id integer;
    v_indice_roleta integer;
    v_premio record;
begin
    if (p_token !~ '^[0-9]{6}$' and p_token !~ '^[A-Za-z0-9_-]{8,32}$')
       or char_length(btrim(p_nome)) not between 2 and 100
       or p_whatsapp !~ '^[0-9]{10,11}$'
       or p_ciencia_privacidade is not true
       or p_consentimento_aniversario is null
       or (p_consentimento_aniversario and p_data_nascimento is null)
       or (not p_consentimento_aniversario and p_data_nascimento is not null)
       or (
           p_data_nascimento is not null
           and p_data_nascimento > (current_date - interval '18 years')::date
       ) then
        raise exception using
            errcode = '22023',
            message = 'Argumentos inválidos para o sorteio.';
    end if;

    select a.id, a.campanha_id, c.texto_consentimento,
           c.politica_privacidade_versao
      into v_acesso_id, v_campanha_id, v_texto_ciencia_privacidade,
           v_politica_privacidade_versao
      from public.acessos_roleta as a
      join public.campanhas as c on c.id = a.campanha_id
     where (a.codigo_curto = p_token or a.token = p_token)
       and a.utilizado_em is null
       and a.cancelado_em is null
       and a.expira_em > now()
       and c.status = 'ativa'
       and (c.data_inicio is null or c.data_inicio <= now())
       and (c.data_fim is null or c.data_fim > now())
     for update of a;

    if v_acesso_id is null then
        if exists (
            select 1 from public.acessos_roleta as a
             where (a.codigo_curto = p_token or a.token = p_token)
               and a.cancelado_em is not null
        ) then
            return query
            select 'token_cancelado', 'Este código foi cancelado.', null::text,
                   null::integer, null::integer;
        elsif exists (
            select 1 from public.acessos_roleta as a
             where (a.codigo_curto = p_token or a.token = p_token)
               and a.utilizado_em is not null
        ) then
            return query
            select 'token_utilizado', 'Este código já foi utilizado!', null::text,
                   null::integer, null::integer;
        elsif exists (
            select 1 from public.acessos_roleta as a
             where (a.codigo_curto = p_token or a.token = p_token)
               and a.expira_em <= now()
        ) then
            return query
            select 'codigo_expirado',
                   'Este código expirou. Peça um novo código no caixa.',
                   null::text, null::integer, null::integer;
        elsif exists (
            select 1 from public.acessos_roleta as a
             where a.codigo_curto = p_token or a.token = p_token
        ) then
            return query
            select 'campanha_inativa', 'Esta campanha não está ativa.', null::text,
                   null::integer, null::integer;
        else
            return query
            select 'token_invalido', 'Código não existe!', null::text,
                   null::integer, null::integer;
        end if;
        return;
    end if;

    perform p.id
      from public.premios as p
     where p.campanha_id = v_campanha_id
       and p.ativo = true
       and p.estoque_disponivel > 0
       and p.peso_sorteio > 0
     order by p.posicao_roleta
     for update;

    select coalesce(sum(p.peso_sorteio), 0)
      into v_total_pesos
      from public.premios as p
     where p.campanha_id = v_campanha_id
       and p.ativo = true
       and p.estoque_disponivel > 0
       and p.peso_sorteio > 0;

    if v_total_pesos <= 0 then
        return query
        select 'sem_premios', 'Acabaram os prêmios no estoque!', null::text,
               null::integer, null::integer;
        return;
    end if;

    v_alvo := random() * v_total_pesos;

    for v_premio in
        select p.id, p.nome, p.peso_sorteio,
               row_number() over (order by p.posicao_roleta)::integer as indice
          from public.premios as p
         where p.campanha_id = v_campanha_id
           and p.ativo = true
         order by p.posicao_roleta
    loop
        if v_premio.peso_sorteio > 0
           and exists (
               select 1 from public.premios as estoque
                where estoque.id = v_premio.id
                  and estoque.estoque_disponivel > 0
           ) then
            v_acumulado := v_acumulado + v_premio.peso_sorteio;
            if v_alvo < v_acumulado then
                v_premio_id := v_premio.id;
                v_premio_nome := v_premio.nome;
                v_indice_roleta := v_premio.indice;
                exit;
            end if;
        end if;
    end loop;

    if v_premio_id is null then
        raise exception using
            errcode = 'P0001',
            message = 'Não foi possível selecionar um prêmio.';
    end if;

    update public.premios
       set estoque_disponivel = estoque_disponivel - 1
     where id = v_premio_id;

    update public.acessos_roleta
       set utilizado_em = clock_timestamp()
     where id = v_acesso_id;

    insert into public.participantes (
        campanha_id, nome, whatsapp, acesso_id, premio_id,
        consentimento_em, politica_privacidade_versao,
        texto_ciencia_privacidade, data_nascimento,
        consentimento_aniversario_em
    ) values (
        v_campanha_id, btrim(p_nome), p_whatsapp, v_acesso_id, v_premio_id,
        clock_timestamp(), v_politica_privacidade_versao,
        v_texto_ciencia_privacidade, p_data_nascimento,
        case when p_consentimento_aniversario
             then clock_timestamp() else null end
    )
    returning id into v_participante_id;

    return query
    select 'sucesso',
           format('Parabéns %s, você ganhou: %s!', btrim(p_nome), v_premio_nome),
           v_premio_nome, v_indice_roleta, v_participante_id;
end;
$$;

create or replace function public.obter_convites_admin(p_campanha_id integer)
returns table (resumo jsonb)
language sql
stable
security invoker
set search_path = public, pg_temp
as $$
    select jsonb_build_object(
        'metricas', jsonb_build_object(
            'convites_total', count(*),
            'convites_pendentes', count(*) filter (
                where utilizado_em is null and cancelado_em is null
                  and expira_em > now()
            ),
            'convites_utilizados', count(*) filter (
                where utilizado_em is not null
            ),
            'convites_cancelados', count(*) filter (
                where cancelado_em is not null
            ),
            'convites_expirados', count(*) filter (
                where utilizado_em is null and cancelado_em is null
                  and expira_em <= now()
            )
        ),
        'convites', coalesce((
            select jsonb_agg(to_jsonb(lista) order by lista.data_criacao desc)
              from (
                  select a.id,
                         coalesce(a.codigo_curto, a.token) as codigo,
                         coalesce(a.codigo_curto, a.token) as token,
                         a.data_criacao, a.expira_em,
                         a.utilizado_em, a.cancelado_em,
                         case
                             when a.cancelado_em is not null then 'cancelado'
                             when a.utilizado_em is not null then 'utilizado'
                             when a.expira_em <= now() then 'expirado'
                             else 'pendente'
                         end as status
                    from public.acessos_roleta as a
                   where a.campanha_id = p_campanha_id
                   order by a.data_criacao desc
                   limit 150
              ) as lista
        ), '[]'::jsonb)
    )
      from public.acessos_roleta
     where campanha_id = p_campanha_id;
$$;

revoke all on function public.gerar_convite_roleta(
    text, text, timestamptz
) from public, anon, authenticated;
revoke all on function public.gerar_convite_roleta(text)
from public, anon, authenticated;
revoke all on function public.verificar_token_roleta(text)
from public, anon, authenticated;
revoke all on function public.sortear_premio_com_privacidade(
    text, text, text, boolean, date, boolean
) from public, anon, authenticated;
revoke all on function public.obter_convites_admin(integer)
from public, anon, authenticated;

grant execute on function public.gerar_convite_roleta(
    text, text, timestamptz
) to service_role;
grant execute on function public.gerar_convite_roleta(text) to service_role;
grant execute on function public.verificar_token_roleta(text) to service_role;
grant execute on function public.sortear_premio_com_privacidade(
    text, text, text, boolean, date, boolean
) to service_role;
grant execute on function public.obter_convites_admin(integer) to service_role;

comment on column public.acessos_roleta.codigo_curto is
'Código numérico de seis dígitos informado à cliente no caixa.';
comment on column public.acessos_roleta.expira_em is
'Momento limite para utilizar o código antes do sorteio.';
