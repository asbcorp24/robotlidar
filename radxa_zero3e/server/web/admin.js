let adminToken=localStorage.getItem('robotlidar_admin_token')||'';
const $=id=>document.getElementById(id);

function adminHeaders(extra={}){return adminToken?{...extra,Authorization:`Bearer ${adminToken}`}:{...extra};}
async function adminApi(url,opts={}){opts.headers=adminHeaders(opts.headers||{});const r=await fetch(url,opts);let data={};try{data=await r.json();}catch{}if(r.status===401){adminLogoutLocal();throw new Error(data.detail||'Требуется вход администратора');}if(!r.ok)throw new Error(data.detail||`HTTP ${r.status}`);return data;}

async function adminLogin(){
  $('adminLoginError').textContent='';
  try{
    const data=await adminApi('/api/admin/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('adminName').value.trim(),password:$('adminPassword').value})});
    adminToken=data.token;
    localStorage.setItem('robotlidar_admin_token',adminToken);
    $('adminPassword').value='';
    await enterAdmin();
  }catch(e){$('adminLoginError').textContent=e.message;}
}

async function enterAdmin(){
  try{
    const me=await adminApi('/api/admin/me');
    $('adminIdentity').textContent=`Администратор: ${me.username}`;
    $('adminLogin').classList.add('hidden');
    $('adminApp').classList.remove('hidden');
    await loadUsers();
  }catch(e){adminLogoutLocal();}
}

function adminLogoutLocal(){
  adminToken='';
  localStorage.removeItem('robotlidar_admin_token');
  $('adminApp').classList.add('hidden');
  $('adminLogin').classList.remove('hidden');
}

async function adminLogout(){try{await adminApi('/api/admin/logout',{method:'POST'});}catch{}adminLogoutLocal();}

async function createUser(){
  $('createUserError').textContent='';
  const username=$('newUsername').value.trim();
  const password=$('newPassword').value;
  try{
    await adminApi('/api/admin/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password})});
    $('newUsername').value='';$('newPassword').value='';
    await loadUsers();
  }catch(e){$('createUserError').textContent=e.message;}
}

async function loadUsers(){
  const data=await adminApi('/api/admin/users',{cache:'no-store'});
  const users=data.users||[];
  $('usersCount').textContent=`Всего: ${users.length}`;
  const list=$('usersList');list.innerHTML='';
  if(!users.length){list.innerHTML='<div class="empty-note">Пользователей пока нет.</div>';return;}
  for(const u of users){
    const row=document.createElement('div');row.className='admin-user';
    const created=u.created_at?new Date(Number(u.created_at)*1000).toLocaleString('ru-RU'):'—';
    row.innerHTML=`<div><strong>${escapeHtml(u.username)}</strong><small>ID ${u.id} · тракторов: ${u.device_count||0} · создан: ${escapeHtml(created)}</small></div><div class="admin-user-actions"><button class="btn secondary password-btn">Сменить пароль</button><button class="btn danger delete-btn">Удалить</button></div>`;
    row.querySelector('.password-btn').onclick=()=>changePassword(u);
    row.querySelector('.delete-btn').onclick=()=>deleteUser(u);
    list.appendChild(row);
  }
}

async function changePassword(u){
  const password=prompt(`Новый пароль для ${u.username}:`);
  if(password===null)return;
  try{
    await adminApi(`/api/admin/users/${encodeURIComponent(u.id)}/password`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});
    alert('Пароль изменён. Активные сессии пользователя завершены.');
  }catch(e){alert(e.message);}
}

async function deleteUser(u){
  if(!confirm(`Удалить пользователя ${u.username}? Его привязки тракторов тоже будут удалены.`))return;
  try{await adminApi(`/api/admin/users/${encodeURIComponent(u.id)}`,{method:'DELETE'});await loadUsers();}catch(e){alert(e.message);}
}

function escapeHtml(s){return String(s??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}

$('adminLoginBtn').onclick=adminLogin;
$('adminLogoutBtn').onclick=adminLogout;
$('createUserBtn').onclick=createUser;
$('refreshUsersBtn').onclick=loadUsers;
$('adminPassword').addEventListener('keydown',e=>{if(e.key==='Enter')adminLogin();});
$('newPassword').addEventListener('keydown',e=>{if(e.key==='Enter')createUser();});
if(adminToken)enterAdmin();
