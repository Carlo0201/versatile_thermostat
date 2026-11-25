param([ValidateSet('prepare','preview','helpers','apply','verify')][string]$Phase='prepare')
$ErrorActionPreference='Stop'
$workspace=(Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$backup=Join-Path $workspace 'tmp/avatto-boiler-before.json'
$prepared=Join-Path $workspace 'tmp/avatto-boiler-proposed.json'
$automation='automation.caldaia_filtro_richiesta_vtherm'
$count='sensor.nb_device_active_for_boiler'
$master='climate.termostato_ingresso'
$manual='binary_sensor.caldaia_richiesta_manuale_oikos'
$shadow='binary_sensor.caldaia_avvio_pilota_avatto'
$rooms='ingresso','soggiorno','cucina','camera_diego_luisa','camera_carlo','camera_laura_livia','bagno_verde','bagno_blu'
$pilot='camera_carlo','bagno_blu'

function New-RoomTemplate($room,$body){
 $session=if($room -in @('camera_carlo','camera_diego_luisa')){"is_state('input_boolean.${room}_pdc_in_gestione','off')"}else{'true'}
 @"
{% set room = 'climate.trv_vtherm_$room' %}
{% set specific = state_attr(room,'specific_states') or {} %}
{% set window = state_attr(room,'window_manager') or {} %}
{% set enabled = is_state('select.central_mode',['Auto','Heat only'])
    and is_state(room,['heat','auto'])
    and state_attr(room,'hvac_action') in ['heating','idle']
    and specific.get('is_initialized',false)
    and not specific.get('inhibition',{}).get('inhibited',false)
    and window.get('window_state') == 'off'
    and $session %}
$body
"@
}

$definitions=[ordered]@{}
foreach($room in $pilot){
 $definitions["binary_sensor.avatto_domanda_$room"]=@{
  name="Avatto Domanda $($room.Replace('_',' '))"
  next_step_id='binary_sensor'
  state=(New-RoomTemplate $room @'
{% set measured = state_attr(room,'current_temperature') | float(none) %}
{% set target = state_attr(room,'temperature') | float(none) %}
{% set previous = this.state if this is defined and this is not none else 'off' %}
{{ enabled and measured is not none and target is not none and
   (((target-measured)|round(3) > 0) if previous == 'on' else ((target-measured)|round(3) >= 0.3)) }}
'@)
 }
 $definitions["sensor.avatto_comando_$room"]=@{
  name="Avatto Comando $($room.Replace('_',' '))"
  next_step_id='sensor'
  state=(New-RoomTemplate $room @"
{% set target = state_attr(room,'temperature') | float(none) %}
{% set source = state_attr(room,'vtherm_over_climate') or {} %}
{% set proposed = source.get('regulation',{}).get('regulated_target_temperature') | float(none) %}
{% if not enabled or target is none %} sospeso
{% elif not is_state('binary_sensor.avatto_domanda_$room','on') %} off
{% else %} {{ [target + 0.5, proposed if proposed is not none else target] | min | round(1) }}
{% endif %}
"@)
 }
}
$rows=@(foreach($room in $rooms){
 $weekend=if($room -eq 'ingresso'){'camera_ingresso'}else{$room}
 "['$room','switch.schedule_${room}_vtherm_feriale','switch.schedule_${weekend}_vtherm_weekend']"
}) -join ",`n"
$manualTemplate=@"
{% set devices = state_attr('$count','active_device_ids') or [] %}
{% set rooms = [$rows] %}
{% set found = namespace(active=false) %}
{% for slug, weekday, weekend in rooms %}
  {% set room = 'climate.trv_vtherm_' ~ slug %}
  {% set timer = 'timer.override_riscaldamento_' ~ slug %}
  {% set explicit = is_state(timer,'active') or
       (is_state(timer,'idle') and is_state(weekday,'off') and is_state(weekend,'off')) %}
  {% set specific = state_attr(room,'specific_states') or {} %}
  {% set window = state_attr(room,'window_manager') or {} %}
  {% set physical = (state_attr(room,'vtherm_over_climate') or {}).get('underlying_entities',[]) %}
  {% set split = slug in ['camera_carlo','camera_diego_luisa'] %}
  {% if explicit and is_state(room,['heat','auto']) and state_attr(room,'hvac_action') == 'heating'
        and specific.get('is_initialized',false)
        and not specific.get('inhibition',{}).get('inhibited',false)
        and window.get('window_state') == 'off'
        and (not split or is_state('input_boolean.' ~ slug ~ '_pdc_in_gestione','off')) %}
    {% for valve in physical if valve in devices and is_state(valve,['heat','auto']) and state_attr(valve,'hvac_action') == 'heating' %}
      {% set found.active = true %}
    {% endfor %}
  {% endif %}
{% endfor %}
{{ found.active and is_state('select.central_mode',['Auto','Heat only']) and states('$count')|int(0) >= 1 }}
"@
$definitions[$manual]=@{name='Caldaia Richiesta manuale Oikos';next_step_id='binary_sensor';state=$manualTemplate}
$definitions[$shadow]=@{name='Caldaia Avvio pilota Avatto';next_step_id='binary_sensor';state=@'
{% set devices = state_attr('sensor.nb_device_active_for_boiler','active_device_ids') or [] %}
{% set pilot = {'climate.trv_camera_carlo':'binary_sensor.avatto_domanda_camera_carlo',
               'climate.trv_bagno_blu':'binary_sensor.avatto_domanda_bagno_blu'} %}
{% set found = namespace(count=0) %}
{% for valve in devices | unique %}
  {% if valve not in pilot or is_state(pilot[valve],'on') %}
    {% set found.count = found.count + 1 %}
  {% endif %}
{% endfor %}
{{ is_state('binary_sensor.caldaia_richiesta_manuale_oikos','on') or
   (found.count >= 2 and is_state('binary_sensor.caldaia_consenso_termico_avvio','on')) }}
'@}

function State-Condition($id,$value){ @{condition='state';entity_id=$id;state=$value} }
function Set-Master($mode){ @{action='climate.set_hvac_mode';target=@{entity_id=$master};data=@{hvac_mode=$mode}} }
function Wait-From($id){
 @{delay=@{seconds="{{ [0, (120 - (now() - states['$id'].last_changed).total_seconds()) | round(0, 'ceil') | int] | max }}"}}
}
function Stable-Condition($id,$value){
 $c=State-Condition $id $value
 $c['for']=@{seconds=120}
 $c
}

$original=Get-Content -Raw -LiteralPath $backup | ConvertFrom-Json -AsHashtable
$config=$original.config
$config.description += ' Una richiesta manuale Oikos con testina attiva può avviare anche da sola dopo 120 secondi.'
$config.triggers+=@{trigger='state';entity_id=$manual;to=$null}
$branches=$config.actions[0].choose
if($branches.Count -ne 3){throw 'Il filtro caldaia è cambiato rispetto alla struttura attesa'}
$start=@{
 alias='Avvia con richiesta manuale Oikos confermata da due minuti'
 conditions=@((State-Condition $manual 'on'),(State-Condition $master 'off'),
  @{condition='numeric_state';entity_id=$count;above=0})
 sequence=@((Wait-From $manual),(Stable-Condition $manual 'on'),(State-Condition $master 'off'),
  @{condition='numeric_state';entity_id=$count;above=0},(Set-Master 'heat'))
}
# Preserve the current stop grace, and let a qualified manual request hold the cycle.
$branches[2].conditions+=@{condition='not';conditions=@((State-Condition $manual 'on'))}
$branches[2].sequence=@((Wait-From $count),(Stable-Condition $count '1'),(State-Condition $master 'heat'),
 @{condition='not';conditions=@((State-Condition $manual 'on'))},(Set-Master 'off'))
$config.actions[0].choose=@($branches[0],$start,$branches[1],$branches[2])

if($Phase -eq 'prepare'){
 $config | ConvertTo-Json -Depth 60 | Set-Content -LiteralPath $prepared -Encoding utf8
 $definitions | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath (Join-Path $workspace 'tmp/avatto-helpers-proposed.json') -Encoding utf8
 Write-Output 'Prepared six helper definitions and a reviewable automation diff.'
 return
}

$taskConfig=Get-Content "$env:USERPROFILE/.codex/config.toml" -Raw
$taskEndpoint=[regex]::Match($taskConfig,'(?s)\[mcp_servers\.home-assistant\].*?url\s*=\s*"([^"]+)"').Groups[1].Value
function Invoke-HaTool($Name,$Arguments){
 $body=@{jsonrpc='2.0';id=1;method='tools/call';params=@{name=$Name;arguments=$Arguments}} | ConvertTo-Json -Depth 70 -Compress
 $response=Invoke-RestMethod -Uri $taskEndpoint -Method Post -ContentType 'application/json' -Headers @{Accept='application/json, text/event-stream'} -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 40
 if($response -is [string]){ $response=(($response -split [char]10 | Where-Object {$_ -like 'data:*'}) -replace '^data:\s*','') | ConvertFrom-Json -AsHashtable }
 if($response.error){throw "RPC error: $($response.error.message)"}
 $result=$response.result
 $data=$result.structuredContent
 if(-not $data -and $result.content[0].text){$data=$result.content[0].text | ConvertFrom-Json -AsHashtable}
 if(-not $data -or $result.isError -or $data.success -eq $false){throw ($data | ConvertTo-Json -Depth 12 -Compress)}
 $data
}
if($Phase -eq 'preview'){
 foreach($id in $definitions.Keys){
  $rendered=Invoke-HaTool 'ha_eval_template' @{template=$definitions[$id].state;strict=$true}
  "$id => $($rendered.result)"
 }
 return
}
if($Phase -eq 'verify'){
 $ids=@($definitions.Keys)+@($automation,$master,$count)
 $r=Invoke-HaTool 'ha_get_state' @{entity_id=$ids}
 foreach($id in $ids){ "$id : $($r.data.states[$id].state)" }
 $live=Invoke-HaTool 'ha_config_get_automation' @{entity_id=$automation}
 $live.config.actions[0].choose.alias
 return
}
$guide=Invoke-HaTool 'ha_get_skill_guide' @{file='SKILL.md'}
$key=[regex]::Match($guide.content,'I-HAVE-READ-THE-BEST-PRACTICES-GUIDE-[0-9a-f]+').Value
if(-not $key){throw 'Missing best-practices acknowledgment key'}
if($Phase -eq 'helpers'){
 foreach($id in $definitions.Keys){
  $existing=Invoke-HaTool 'ha_eval_template' @{template="{{ states['$id'] is defined and states['$id'] is not none }}"}
  if("$($existing.result)" -eq 'True'){ "$id already exists";continue }
  $entry=$definitions[$id].Clone()
  $name=$entry.name
  $entry.Remove('name')
  $r=Invoke-HaTool 'ha_config_set_helper' @{helper_type='template';name=$name;config=$entry;BestPracticeKey=$key}
  "$id created ($($r.entry_id))"
  $current=Invoke-HaTool 'ha_eval_template' @{template="{{ states('$id') }}"}
  if("$($current.result)" -in @('unknown','unavailable','')){throw "Expected helper $id not available"}
 }
 return
}
$manualState=Invoke-HaTool 'ha_eval_template' @{template="{{ states('$manual') }}"}
if("$($manualState.result)" -notin @('on','off')){throw 'Il consenso manuale non è pronto'}
$live=Invoke-HaTool 'ha_config_get_automation' @{entity_id=$automation}
if($live.config_hash -ne $original.config_hash){throw 'Il filtro in Home Assistant è cambiato dopo il backup; rileggere prima di applicare'}
$r=Invoke-HaTool 'ha_config_set_automation' @{identifier=$automation;config_hash=$original.config_hash;config=$config;BestPracticeKey=$key}
$r | Select-Object success, automation_id, action, best_practice_warnings | ConvertTo-Json -Depth 12 -Compress

