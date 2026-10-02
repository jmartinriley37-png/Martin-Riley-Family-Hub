const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const app={innerHTML:'',modal:''};
const context=vm.createContext({
 document:{querySelector:()=>app,getElementById:()=>null,body:{insertAdjacentHTML:(_position,html)=>{app.modal=html}},addEventListener(){},visibilityState:'visible'},
 window:{scrollTo(){}},localStorage:{removeItem(){}},
 fetch:()=>new Promise(()=>{}),setInterval(){},setTimeout(){},console,
 alert(){},prompt(){return null},confirm(){return false}
});
vm.runInContext(fs.readFileSync('app.js','utf8'),context);
assert.match(app.innerHTML,/Welcome home/);
vm.runInContext("S.profiles={Dad:{displayName:'Jermaine',role:'ADMIN'},Mom:{displayName:'Stephanie',role:'ADMIN'},Daughter:{displayName:'Arielle',role:'CHILD'}};loginScreen()",context);
assert.match(app.innerHTML,/value="Dad">Jermaine/);
assert.match(app.innerHTML,/value="Mom">Stephanie/);
assert.match(app.innerHTML,/value="Daughter">Arielle/);
vm.runInContext("S={viewer:'Dad',tasks:[],posts:[],requests:[],events:[],lists:[],dance:[],notes:[],activity:[],streak:0,points:0}",context);
vm.runInContext("S.profiles={Dad:{displayName:'Jermaine',role:'ADMIN'},Mom:{displayName:'Stephanie',role:'ADMIN'},Daughter:{displayName:'Arielle',role:'CHILD'}}",context);
for(const role of ['Dad','Mom','Daughter']){
 vm.runInContext("S.viewer='"+role+"'",context);
 for(const page of ['home','tasks','board','ask','requests','dance','calendar','tomorrow','recap','family','privacy','lists','private','vault','reminders']){
  vm.runInContext("render('"+page+"')",context);
  assert.match(app.innerHTML,/Martin-Riley Family Hub/);
  assert.doesNotMatch(app.innerHTML,/undefined|NaN/);
 }
}
vm.runInContext("S.viewer='Dad';render('tasks');openAdd()",context);
for(const field of ['id=nt','id=ndesc','id=nw','id=np','id=nv','id=nd','id=ntime','id=nc','id=nr','id=na']) assert.ok(app.modal.includes(field),field);
for(const choice of ['Jermaine','Stephanie','Arielle','Everyone','Unassigned','High Priority','Weekdays','Appointments']) assert.ok(app.modal.includes(choice),choice);
vm.runInContext("S.viewer='Dad';render('home')",context);
for(const action of ['+ Task','+ Event','+ List Item','+ Note']) assert.ok(app.innerHTML.includes(action),action);
vm.runInContext("S.activity=[{summary:'✅ Arielle completed \\\"Take out trash\\\"',createdAt:'2026-10-02T00:02:00Z'}];render('home')",context);
assert.ok(app.innerHTML.includes('Recent Activity')&&app.innerHTML.includes('Arielle completed'));
vm.runInContext("S.viewer='Daughter';render('home');render('ask')",context);
assert.ok(!app.innerHTML.includes('+ Task'));
assert.ok(app.innerHTML.includes('Ask Jermaine &amp; Stephanie')||app.innerHTML.includes('Ask Jermaine & Stephanie'));
vm.runInContext("S.viewer='Dad';S.activity=['Jermaine created a task'];render('family')",context);
assert.ok(app.innerHTML.includes('Jermaine')&&app.innerHTML.includes('Stephanie')&&app.innerHTML.includes('Arielle'));
vm.runInContext("S.viewer='Dad';taskFilterState.status='All';S.tasks=[{id:41,title:'Take out trash',description:'Blue bin',who:'Daughter',creator:'Dad',priority:'Urgent',visibility:'Family',category:'Home',dueDate:'2026-10-03',dueTime:'12:02',repeat:'Weekly',ack:true,acked:['Daughter'],acknowledgements:[{person:'Daughter',acknowledgedAt:'2026-10-02T00:00:00Z'}],status:'done',completedBy:'Daughter',completedAt:'2026-10-02T00:02:00Z',createdAt:'2026-10-01T12:00:00Z',updatedAt:'2026-10-02T00:02:00Z',updatedBy:'Daughter',completionHistory:[{completedBy:'Daughter',completedAt:'2026-10-02T00:02:00Z'}]}];render('tasks');openTaskDetails(41)",context);
for(const text of ['role=button','Completed by','Edit / Reassign','Reopen','Delete','Acknowledged by','Completion history','Last updated','Completion status']) assert.ok((text==='role=button'?app.innerHTML:app.modal).includes(text),text);
vm.runInContext("confirmDeleteTask(41)",context);
assert.ok(app.modal.includes('Delete “Take out trash”?')&&app.modal.includes('Delete Task')&&app.modal.includes('Cancel'));
vm.runInContext("S.viewer='Dad';S.tasks=[{id:43,title:'Pack dance bag',who:'Daughter',creator:'Dad',priority:'Normal',visibility:'Family',category:'Home',repeat:'One Time',ack:true,acked:['Daughter'],status:'open'}];openTaskDetails(43)",context);
assert.ok(app.modal.includes('Mark Complete')&&app.modal.includes('Mark Not Complete'));
vm.runInContext("S.viewer='Daughter';S.tasks=[{id:42,title:'Pack dance bag',description:'',who:'Daughter',creator:'Dad',priority:'Normal',visibility:'Family',category:'Home',repeat:'One Time',ack:false,acked:[],status:'open',createdAt:'2026-10-01T12:00:00Z'}];openTaskDetails(42)",context);
assert.ok(!app.modal.includes('Edit / Reassign')&&!app.modal.includes('Delete Task'));
vm.runInContext("openMissReason(42,false)",context);
for(const reason of ['Ran out of time','Waiting on someone/something','Rescheduled','No longer needed','Other']) assert.ok(app.modal.includes(reason),reason);
vm.runInContext("S.viewer='Dad';render('tasks')",context);
for(const filter of ['Status','Assigned','Priority','Category','Completed','Not Completed']) assert.ok(app.innerHTML.includes(filter),filter);
vm.runInContext("S.viewer='Daughter';S.tasks=[{id:44,title:'Arielle chore',who:'Daughter',creator:'Dad',priority:'Normal',visibility:'Family',category:'Home',ack:false,acked:[],status:'open'}];render('tasks')",context);
assert.ok(!app.innerHTML.includes('task-filters')&&app.innerHTML.includes('task-card')&&app.innerHTML.includes('Arielle chore'));
vm.runInContext("S.posts=[{id:1,by:'Mom',text:safeState('<img src=x onerror=alert(1)>'),reacts:{}}];render('board')",context);
assert.ok(!app.innerHTML.includes('<img src=x'));
assert.ok(app.innerHTML.includes('&lt;img'));
console.log('PASS: all screens render for three roles; user HTML is escaped.');
