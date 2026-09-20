from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION plane_guard_workflow_issue() RETURNS trigger AS $$
DECLARE cfg record;
BEGIN
  IF current_setting('plane.workflow_command', true) = 'on' THEN
    RETURN NEW;
  END IF;
  IF TG_OP = 'UPDATE' THEN
    IF NEW.state_id IS NOT DISTINCT FROM OLD.state_id
       AND NEW.project_id IS NOT DISTINCT FROM OLD.project_id
       AND NEW.deleted_at IS NOT DISTINCT FROM OLD.deleted_at THEN
      RETURN NEW;
    END IF;
    IF EXISTS (SELECT 1 FROM db_workflowconfiguration WHERE project_id = OLD.project_id AND enabled) THEN
      RAISE EXCEPTION 'workflow_command_required: use guarded versioned commands' USING ERRCODE = '23514';
    END IF;
  END IF;
  SELECT * INTO cfg FROM db_workflowconfiguration WHERE project_id = NEW.project_id AND enabled;
  IF FOUND THEN
    IF TG_OP = 'INSERT' AND NEW.state_id::text IN (cfg.state_mapping->>'Backlog', cfg.state_mapping->>'Todo') THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION 'workflow_command_required: use guarded versioned commands' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER plane_workflow_issue_guard BEFORE INSERT OR UPDATE ON issues
FOR EACH ROW EXECUTE FUNCTION plane_guard_workflow_issue();
"""
REVERSE = "DROP TRIGGER IF EXISTS plane_workflow_issue_guard ON issues; DROP FUNCTION IF EXISTS plane_guard_workflow_issue();"


class Migration(migrations.Migration):
    dependencies = [('db', '0134_usage_delivery_identity')]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
