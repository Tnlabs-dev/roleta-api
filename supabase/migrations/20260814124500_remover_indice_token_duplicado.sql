set lock_timeout = '5s';
set statement_timeout = '30s';

do $$
begin
    if to_regclass('public.acessos_roleta_token_key') is not null
       and to_regclass('public.acessos_roleta_token_unico_idx') is not null then
        drop index public.acessos_roleta_token_unico_idx;
    end if;
end;
$$;
