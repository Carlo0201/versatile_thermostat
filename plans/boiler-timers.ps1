param([ValidateSet('prepare','preview','helpers','apply','verify')][string]$Phase='prepare')
$ErrorActionPreference='Stop'
$workspace=(Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$start='timer.caldaia_attesa_avvio'
$stop='timer.caldaia_attesa_spegnimento'
$master='climate.termostato_ingresso'
$count='sensor.nb_device_active_for_boiler'
$manual='binary_sensor.caldaia_richiesta_manuale_oikos'
$threshold='binary_sensor.central_configuration_caldaia_soglia_due_testine'
$filter='automation.caldaia_filtro_richiesta_vtherm'
$id='caldaia_sincronizza_timer'
$observer='automation.caldaia_sincronizza_timer'

$startDeadline=@"
{% if is_state('$filter','on') and is_state('$master','off') %}
  {% if is_state('$manual','on') and states('$count')|int(0) > 0 %}
    {{ as_timestamp(states['$manual'].last_changed) + 120 }}
  {% elif is_state('$threshold','on') and states('$count')|int(0) > 1 %}
    {{ as_timestamp(states['$threshold'].last_changed) + 120 }}
  {% else %}0{% endif %}
{% else %}0{% endif %}
"@
$stopDeadline=@"
{% if is_state('$filter','on') and is_state('$master','heat')
      and is_state('$count','1') and not is_state('$manual','on') %}
  {{ as_timestamp(states['$count'].last_changed) + 120 }}
{% else %}0{% endif %}
"@

function State-Condition($entity,$state){@{condition='state';entity_id=$entity;state=$state}}
function Sync-Timer($entity,$deadline){
 # Keep the original deadline when HA restarts or a watched state changes.
 @{
  choose=@(
   @{
    conditions=@(
     @{condition='template';value_template="{{ (remaining_seconds | int(0)) > 0 }}"}
     @{condition='template';value_template="{{ not is_state('$entity','active') or ((as_timestamp(state_attr('$entity','finishes_at'),0) - ($deadline | float(0))) | abs > 2) }}"}
    )
    sequence=@(@{action='timer.start';target=@{entity_id=$entity};data=@{duration='{{ remaining_seconds | int(0) }}'}})
   },
   @{
    conditions=@(
     @{condition='template';value_template="{{ (remaining_seconds | int(0)) == 0 }}"}
     (State-Condition $entity @('active','paused'))
    )
    sequence=@(@{action='timer.cancel';target=@{entity_id=$entity}})
   }
  )
 }
}
# Use separate variables for each timer's remaining time. Only this observer
# writes the timers. Timer events never drive the thermostat.
$config=@{
 id=$id
 alias='Caldaia sincronizza timer'
 description='Mostra le attese del filtro caldaia in due timer Home Assistant. Le scadenze vengono ricalcolate dagli stati del filtro, senza inviare comandi alla caldaia.'
 mode='queued'
 max=20
 triggers=@(
  @{trigger='state';entity_id=@($filter,$master,$count,$manual,$threshold);to=$null},
  @{trigger='homeassistant';event='start'},
  @{trigger='event';event_type='automation_reloaded'}
 )
 actions=@(
  @{variables=@{start_deadline=$startDeadline;stop_deadline=$stopDeadline}},
  @{variables=@{remaining_seconds='{{ [0, ((start_deadline | float(0)) - as_timestamp(now())) | round(0,"ceil") | int] | max }}'}},
  (Sync-Timer $start 'start_deadline'),
  @{variables=@{remaining_seconds='{{ [0, ((stop_deadline | float(0)) - as_timestamp(now())) | round(0,"ceil") | int] | max }}'}},
  (Sync-Timer $stop 'stop_deadline')
 )
}
if($Phase -eq 'prepare'){
 $config | ConvertTo-Json -Depth 25 | Set-Content -LiteralPath (Join-Path $workspace 'tmp/boiler-timers-proposed.json') -Encoding utf8
 return
}
$transport=Get-Content -Raw (Join-Path $PSScriptRoot 'avatto-oikos-pilot.ps1')
$begin=$transport.IndexOf('$taskConfig=')
$end=$transport.IndexOf("if(`$Phase -eq 'preview')",$begin)
. ([scriptblock]::Create($transport.Substring($begin,$end-$begin)))

if($Phase -eq 'preview'){
 foreach($template in $startDeadline,$stopDeadline){
  $r=Invoke-HaTool 'ha_eval_template' @{template=$template;strict=$true}
  "Deadline: $($r.result)"
 }
 return
}
if($Phase -eq 'verify'){
 $entities=@($start,$stop,$observer,$filter,$master,$count,$manual)
 $r=Invoke-HaTool 'ha_get_state' @{entity_id=$entities}
 foreach($entity in $entities){
  $s=$r.data.states[$entity]
  "$entity | $($s.state) | $($s.attributes.duration) | $($s.attributes.finishes_at) | restore=$($s.attributes.restore)"
 }
 return
}
$guide=Invoke-HaTool 'ha_get_skill_guide' @{file='SKILL.md'}
$key=[regex]::Match($guide.content,'I-HAVE-READ-THE-BEST-PRACTICES-GUIDE-[0-9a-f]+').Value
if(-not $key){throw 'Missing best-practices acknowledgment key'}
if($Phase -eq 'helpers'){
 foreach($item in @(@{id=$start;name='Caldaia attesa avvio'},@{id=$stop;name='Caldaia attesa spegnimento'})){
  $r=Invoke-HaTool 'ha_eval_template' @{template="{{ states['$($item.id)'] is not none }}"}
  if("$($r.result)" -eq 'True'){ "$($item.id) exists"; continue }
  $r=Invoke-HaTool 'ha_config_set_helper' @{helper_type='timer';name=$item.name;icon='mdi:timer-outline';config=@{duration='00:02:00';restore=$true};BestPracticeKey=$key}
  "$($item.id): $($r.success)"
 }
 return
}
$live=Invoke-HaTool 'ha_get_state' @{entity_id=@($start,$stop,$observer)}
foreach($entity in $start,$stop){
 if($live.data.states[$entity].state -notin @('active','paused','idle')){throw "Timer non disponibile: $entity"}
}
if($live.data.states[$observer].state){throw "L'automazione esiste già: $observer"}
$r=Invoke-HaTool 'ha_config_set_automation' @{config=$config;BestPracticeKey=$key}
$r | Select-Object success,automation_id,best_practice_warnings | ConvertTo-Json -Depth 10 -Compress

