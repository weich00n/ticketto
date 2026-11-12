output "experiment_templates" {
  description = "FIS experiment template IDs, by experiment."
  value = merge(
    { for k, t in aws_fis_experiment_template.stop_service : k => t.id },
    {
      c1 = aws_fis_experiment_template.stop_broker.id
      c2 = aws_fis_experiment_template.reboot_reservation_redis.id
    },
  )
}

output "driver_security_group_id" {
  description = "Add to teammates' client security group variables so the driver can reach their APIs."
  value       = aws_security_group.driver.id
}

output "abort_alarm" {
  value = aws_cloudwatch_metric_alarm.abort.alarm_name
}
